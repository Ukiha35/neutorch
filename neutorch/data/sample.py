from __future__ import annotations
import os
from abc import ABC, abstractmethod, abstractproperty
import random
from typing import List, Union
from functools import cached_property

import numpy as np
from yacs.config import CfgNode

from chunkflow.lib.cartesian_coordinate import BoundingBox, Cartesian, BoundingBoxes
from chunkflow.chunk import Chunk
from chunkflow.volume import load_chunk_or_volume
from chunkflow.lib.synapses import Synapses
from chunkflow.volume import PrecomputedVolume, AbstractVolume


from neutorch.data.patch import Patch
# from .patch_bounding_box_generator import PatchBoundingBoxGeneratorInChunk, PatchBoundingBoxGeneratorInsideMask
from neutorch.data.transform import *

DEFAULT_PATCH_SIZE = Cartesian(128, 128, 128)
DEFAULT_NUM_CLASSES = 1


class AbstractSample(ABC):
    def __init__(self, output_patch_size: Cartesian, is_train: bool = True):

        if isinstance(output_patch_size, int):
            output_patch_size = (output_patch_size,) * 3
        else:
            assert len(output_patch_size) == 3

        if not isinstance(output_patch_size, Cartesian):
            output_patch_size = Cartesian.from_collection(output_patch_size)
        self.output_patch_size = output_patch_size
        self.is_train = is_train

    @property
    @abstractmethod
    def random_patch(self):
        pass

    @property
    def sampling_weight(self) -> int:
        """the weight to sample 

        Returns:
            int: the relative weight. The default is 1, 
                so all the sample have the same weight.
        """
        return 1 

    def __len__(self):
        """number of patches
        we simplly return a number as default value to make it work with distributed sampler.
        """
        return 64

    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            # AdjustContrast(factor_range = (0.95, 1.8)),
            # AdjustBrightness(min_factor = 0.05, max_factor = 0.2),
            AdjustContrast(),
            AdjustBrightness(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            MaskBox(),
            # Perspective2D(),
            # RotateScale(probability=1.),
            DropSection(probability=1.),
            Flip(),
            Transpose(),
            # MissAlignment(),
            Label2AffinityMap(), 
        ])

    @cached_property
    def patch_size_before_transform(self):
        return self.output_patch_size + \
            self.transform.shrink_size[:3] + \
            self.transform.shrink_size[-3:]

# class BlockAlignedVolumeSample(AbstractSample):
#     def __init__(self, 
#             images: List[AbstractVolume],
#             label: AbstractVolume,
#             output_patch_size: Cartesian,
#             forbbiden_distance_to_boundary: tuple = None,
#         ):
#         """sample patches inside blocks of volume
#         This will reduce the cost of reading and decompression by avoiding patches cross blocks

#         Args:
#             images (List[AbstractVolume]): image volumes.
#             label (AbstractVolume): the label volume.
#             output_patch_size (Cartesian): output patch size.
#             forbbiden_distance_to_boundary (tuple, optional): minimum distance to boundary. Defaults to None.
#         """
#         super().__init__(output_patch_size=output_patch_size)
#         self.images = images
#         self.label = label

#         self.patch_bbox_generator = PatchBoundingBoxGenerator(
#             self.patch_size_before_transform,
#             self.images[0].bounding_box,
#             forbbiden_distance_to_boundary=forbbiden_distance_to_boundary,
#         )

#     @property
#     def random_patch(self):
#         bbox = self.patch_bbox_generator.random_patch_bbox
#         image_volume = random.choice(self.images)
#         patch_image = image_volume.cutout(bbox)
#         patch_label = self.label.cutout(bbox)
#         return Patch(patch_image, patch_label)


class Sample(AbstractSample):
    def __init__(self, 
            images: List[Chunk | PrecomputedVolume],
            label: Chunk | PrecomputedVolume,
            output_patch_size: Cartesian, 
            forbbiden_distance_to_boundary: tuple = None,
            is_train: bool = True) -> None:
        """Image sample with ground truth annotations

        Args:
            images (List[Chunk]): different versions of image chunks normalized to 0-1
            label (np.ndarray): training label
            patch_size (Cartesian): output patch size. this should be the patch_size before transform. 
                the patch is expected to be shrinked to be the output patch size.
            forbbiden_distance_to_boundary (Union[tuple, int]): 
                the distance from patch center to sample boundary that is not allowed to sample 
                the order is z,y,x,-z,-y,-x
                if this is an integer, then all dimension is the same.
                if this is a tuple of three integers, the positive and negative is the same
                if this is a tuple of six integers, the positive and negative 
                direction is defined separately.
            is_train (bool): train mode or validation mode. We'll skip the transform in validation mode. 
        """
        super().__init__(output_patch_size=output_patch_size)
        assert len(images) > 0
        # assert images[0].ndim >= 3
        # assert label.ndim >= 3
        # assert isinstance(label, Chunk)
        # for image in images:
        #     assert isinstance(image, Chunk)
        # assert images[0].shape[-3:] == label.shape[-3:], f'label voxel offset: {label.shape}'
        
        # if isinstance(label, Chunk):
            # label = label.array
        
        self.images = images
        self.label = label
        
        assert isinstance(self.output_patch_size, Cartesian)
        # for ps, ls in zip(self.output_patch_size, label.shape[-3:]):
        #     assert ls >= ps, f'output patch size: {self.output_patch_size}, label shape: {label.shape}'

        if forbbiden_distance_to_boundary is None:
            forbbiden_distance_to_boundary = self.patch_size_before_transform // 2 
        assert len(forbbiden_distance_to_boundary) == 3 or len(forbbiden_distance_to_boundary)==6

        for idx in range(3):
            # the center of random patch should not be too close to boundary
            # otherwise, the patch will go outside of the volume
            assert forbbiden_distance_to_boundary[idx] >= self.patch_size_before_transform[idx] // 2
            assert forbbiden_distance_to_boundary[-idx] >= self.patch_size_before_transform[-idx] // 2
        
        self.center_start = forbbiden_distance_to_boundary[:3]
        self.center_stop = tuple(s - d for s, d in zip(
            images[0].shape[-3:], forbbiden_distance_to_boundary[-3:]))

        self.center_start = Cartesian.from_collection(self.center_start)
        self.center_stop = Cartesian.from_collection(self.center_stop)

        # for cs, cp in zip(self.center_start, self.center_stop):
        #     assert cp > cs, \
        #         f'center start: {self.center_start}, center stop: {self.center_stop}'

    # @classmethod
    # def from_json(cls, json_file: str, patch_size: Cartesian = DEFAULT_PATCH_SIZE):
    #     with open(json_file, 'r') as jf:
    #         data = json.load(jf)
    #     return cls.from_dict(data, patch_size=patch_size)

    def _expand_to_5d(self, array: np.ndarray):
        if array.ndim == 3:
            return np.expand_dims(array, axis=(0, 1))
        elif array.ndim == 4:
            return np.expand_dims(array, axis=0)
        elif array.ndim == 5:
            return array
        else:
            raise ValueError('only support 3 to 5 dimensional array.')
   
    @property
    def random_patch_center(self): 
        center_start = self.center_start
        center_stop = self.center_stop
        cz = random.randrange(center_start[0], center_stop[0])
        cy = random.randrange(center_start[1], center_stop[1])
        cx = random.randrange(center_start[2], center_stop[2])
        if cx > 7198: #For good regions
            pass
        else:
            center = Cartesian(cz, cy, cx)
            return center

    def __len__(self):
        patch_num = np.prod(self.center_stop - self.center_start + 1)
        return patch_num

    def patch_from_center(self, center: Cartesian):
        start = center - self.patch_size_before_transform // 2
        bbox = BoundingBox.from_delta(start, self.patch_size_before_transform)
        
        image = random.choice(self.images)
        bbox += image.bbox.start
        image_patch = image.cutout(bbox)
        label_patch = self.label.cutout(bbox)
        
        if image_patch.shape[-3:] != self.patch_size_before_transform.tuple:
            print(f'center: {center}, start: {start}, bbox: {bbox}')
        
        # print(f'start: {(bz, by, bx)}, patch size: {self.output_patch_size}')
        assert image_patch.shape[-1] == image_patch.shape[-2], f'image patch shape: {image_patch.shape}'
        assert image_patch.shape[-3:] == self.patch_size_before_transform.tuple, \
            f'image patch shape: {image_patch.shape}, patch size before transform: {self.patch_size_before_transform}'
        # if we do not copy here, the augmentation will change our 
        # image and label sample!
        image_patch.array = self._expand_to_5d(image_patch.array).copy()
        label_patch.array = self._expand_to_5d(label_patch.array).copy()

        return Patch(image_patch, label_patch)
    
    @property
    def random_patch(self):
        patch = self.patch_from_center(self.random_patch_center)
        #breakpoint() 
        print(f'transforms: {self.transform}') 
        print(f'patch size before transform: {patch.shape}')
        # print(f'transforms: {self.transform}') 
        # print(f'patch size before transform: {patch.shape}')
        # skip the transform in validation mode
        if self.is_train:
            self.transform(patch)
        #print(f'patch size after transform: {patch.shape}')
        #breakpoint() 
        #self.transform(patch)
        print(f'patch size after transform: {patch.shape}')
        assert patch.shape[-3:] == self.output_patch_size, \
            f'get patch shape: {patch.shape}, expected patch size {self.output_patch_size}'
        return patch
    
    @cached_property
    def sampling_weight(self):
        """voxel number of label"""
        weight = int(np.product(tuple(e-b for b, e in zip(
            self.center_start, self.center_stop))))
        
        # if len(np.unique(self.label)) == 1:
        #     # reduce the weight
        #     weight /= 10.

        return weight
    

class SampleWithMask(Sample):
    def __init__(self, 
            images: List[PrecomputedVolume],
            label: Union[Chunk, PrecomputedVolume],
            output_patch_size: Cartesian,
            mask: Chunk | PrecomputedVolume, 
            forbbiden_distance_to_boundary: tuple = None,
            patches_in_block: int = 8,
            nonzero_bounding_boxes_path: str = './nonzero_bounding_boxes.npy',
            ) -> None:
        """Image sample with ground truth annotations

        Args:
            images (List[Chunk]): different versions of image chunks normalized to 0-1
            label (np.ndarray): training label
            output_patch_size (Cartesian): output patch size. this should be the patch_size before transform. 
                the patch is expected to be shrinked to be the output patch size.
            mask (Chunk | PrecomputedVolume): neuropil mask that indicates inside of neuropil.
            forbbiden_distance_to_boundary (Union[tuple, int]): 
                the distance from patch center to sample boundary that is not allowed to sample 
                the order is z,y,x,-z,-y,-x
                if this is an integer, then all dimension is the same.
                if this is a tuple of three integers, the positive and negative is the same
                if this is a tuple of six integers, the positive and negative 
                direction is defined separately. 
            patches_in_block (int): sample a number of patches in a block.
            nonzero_bounding_boxes_path (str): 
        """
        super().__init__(
            images, label, output_patch_size=output_patch_size, 
            forbbiden_distance_to_boundary=forbbiden_distance_to_boundary)
        assert patches_in_block > 0
        self.mask = mask
        self.patch_number = 0
        self.patches_in_block = patches_in_block
        self.image_block = None
        self.label_block = None
        self.nonzero_bounding_boxes_path = nonzero_bounding_boxes_path

    @classmethod
    def from_config(cls, config: CfgNode, 
            output_patch_size: Cartesian) -> SampleWithMask:
        images = []
        for image_path in config.images:
            image_vol = PrecomputedVolume.from_cloudvolume_path(image_path)
            images.append(image_vol)
        
        label = PrecomputedVolume.from_cloudvolume_path(config.label)
        mask = PrecomputedVolume.from_cloudvolume_path(config.mask)
        return cls(images, label, output_patch_size, mask)

    @cached_property
    def voxel_size_factors(self) -> Cartesian:
        return self.mask.voxel_size // self.images[0].voxel_size 

    @cached_property
    def nonzero_block_bounding_boxes(self) -> BoundingBoxes:
        if os.path.exists(self.nonzero_bounding_boxes_path):
            print(f'loading existing nonzero bounding boxes file: {self.nonzero_bounding_boxes_path}')
            bboxes = BoundingBoxes.from_file(self.nonzero_bounding_boxes_path)
        else:
            bboxes = self.mask.get_nonzero_block_bounding_boxes_with_different_voxel_size(
                self.label.voxel_size
            )
            bboxes.to_file(self.nonzero_bounding_boxes_path)
        return bboxes 

    @property
    def random_block_pair(self) -> BoundingBox:
        image_volume = random.choice(self.images)
        # the block in mask is pretty big since it is normally in high mip level
        # we should use the image or label mip level to get the block bounding box
        # list in the highest mip level to increase the number of available blocks
        # with all nonzero mask!
        image_block_bbox = random.choice(self.nonzero_block_bounding_boxes)
        image_block = image_volume.cutout(image_block_bbox)
        label_block = self.label.cutout(image_block_bbox)
        assert image_block.shape[-3:] == label_block.shape[-3:]
        return (image_block, label_block)

    @property
    def random_patch(self):
        if self.patch_number % self.patches_in_block == 0:
            self.image_block, self.label_block = self.random_block_pair
        start_stop = self.image_block.stop - self.patch_size_before_transform
        start_bbox = BoundingBox(self.image_block.start, start_stop)
        start = start_bbox.random_coordinate
        patch_bbox = BoundingBox.from_delta(start, self.patch_size_before_transform)
        image_patch = self.image_block.cutout(patch_bbox)
        label_patch = self.label_block.cutout(patch_bbox)
        patch = Patch(image_patch, label_patch)
        self.transform(patch)
        return patch

    @cached_property
    def sampling_weight(self) -> int:
        block_num = len(self.nonzero_block_bounding_boxes)
        block_size = self.label.block_size * self.voxel_size_factors
        return np.product(block_size) * block_num

    def __len__(self):
        patch_num = len(self.nonzero_block_bounding_boxes) * \
            np.prod(self.label.block_size - self.patch_size_before_transform + 1)
        return patch_num


class SampleWithPointAnnotation(Sample):
    def __init__(self, 
            images: List[Chunk], 
            annotation_points: np.ndarray,
            output_patch_size: Cartesian, 
            forbbiden_distance_to_boundary: tuple = None) -> None:
        """Image sample with ground truth annotations

        Args:
            image (np.ndarray): image normalized to 0-1
            annotation_points (np.ndarray): point annotations with zyx order.
            output_patch_size (Cartesian): output patch size
            forbbiden_distance_to_boundary (tuple, optional): sample patches far away 
                from sample boundary. Defaults to None.
        """

        assert annotation_points.shape[1] == 3 #check 
        self.annotation_points = annotation_points
        label = np.zeros_like(images[0].array, dtype=np.float32)
        label = self._points_to_label(label)
        super().__init__(
            images, label, 
            output_patch_size = output_patch_size,
            forbbiden_distance_to_boundary=forbbiden_distance_to_boundary
        )

    @property
    def sampling_weight(self):
        """use number of annotated points as weight to sample volume."""
        return int(self.annotation_points.shape[0])

    def _points_to_label(self, label: np.ndarray,
            expand_distance: int = 2) -> tuple:
        """transform point annotation to volumes

        Args:
            expand_distance (int): expand the point annotation to a cube. 
                This will help to got more positive voxels.
                The expansion should be small enough to ensure that all the voxels are inside T-bar.

        Returns:
            bin_presyn: binary label of annotated position.
        """
        # assert synapses['resolution'] == [8, 8, 8]
        # label = np.zeros_like(image, dtype=np.float32)
        # adjust label to 0.05-0.95 for better regularization
        # the effect might be similar with Focal loss!
        label += 0.05
        for idx in range(self.annotation_points.shape[0]):
            coordinate = self.annotation_points[idx, :]
            label[...,
                coordinate[0]-expand_distance : coordinate[0]+expand_distance,
                coordinate[1]-expand_distance : coordinate[1]+expand_distance,
                coordinate[2]-expand_distance : coordinate[2]+expand_distance,
            ] = 0.95
        assert np.any(label > 0.5)
        return label


class PostSynapseReference(AbstractSample):
    def __init__(self,
            synapses: Synapses,
            images: List[Chunk], 
            output_patch_size: Cartesian, 
            point_expand: int = 2,
        ):
        """Ground Truth for post synapses

        Args:
            synapses (Synapses): including both presynapses and postsynapses
            images (List[Chunk]): several image chunk versions covering the whole synapses
            patch_size (Cartesian): image patch size covering the whole synapse
            point_expand (int): expand the point. range from 1 to half of patch size.
        """
        super().__init__(output_patch_size=output_patch_size)

        self.images = images
        self.synapses = synapses
        self.pre_index2post_indices = synapses.pre_index2post_indices
        self.point_expand = point_expand

    @property
    def random_patch(self):
        pre_index = random.randrange(0, self.synapses.pre_num)
        pre = self.synapses.pre[pre_index, :]
        
        post_indices = self.pre_index2post_indices[pre_index]
        assert len(post_indices) > 0

        bbox = BoundingBox.from_center(
            Cartesian(*pre), 
            extent=self.output_patch_size // 2
        )

        image = random.choice(self.images)
        
        # Note that image is 4D array, the first dimension size is 1
        image = image.cutout(bbox)
        assert image.dtype == np.uint8
        image = image.astype(np.float32)
        image /= 255.
        # pre_label = np.zeros_like(image)
        # pre_label[
            
        #     pre[0] - self.point_expand : pre[0] + self.point_expand,
        #     pre[1] - self.point_expand : pre[1] + self.point_expand,
        #     pre[2] - self.point_expand : pre[2] + self.point_expand,
        # ] = 0.95

        # stack them together in the channel dimension
        # image = np.expand_dims(image, axis=0)
        # pre_label = np.expand_dims(pre_label, axis=0)
        # image = np.concatenate((image, pre_label), axis=0)

        label = np.zeros(image.shape, dtype=np.float32)
        label = Chunk(label, voxel_offset=image.voxel_offset)
        label += 0.05
        for post_index in post_indices:
            assert post_index < self.synapses.post_num
            coord = self.synapses.post_coordinates[post_index, :]
            coord = coord - label.voxel_offset
            label[...,
                coord[0] - self.point_expand : coord[0] + self.point_expand,
                coord[1] - self.point_expand : coord[1] + self.point_expand,
                coord[2] - self.point_expand : coord[2] + self.point_expand,
            ] = 0.95
        assert np.any(label > 0.5)

        return Patch(image, label)

    def __len__(self):
        return self.synapses.pre_num


class SemanticSample(Sample):
    def __init__(self, 
            images: List[Chunk], 
            label: Union[np.ndarray, Chunk], 
            output_patch_size: Cartesian,
            num_classes: int = DEFAULT_NUM_CLASSES,
            forbbiden_distance_to_boundary: tuple = None) -> None:
        super().__init__(images, label, output_patch_size, forbbiden_distance_to_boundary)
        # number of classes
        self.num_classes = num_classes

    @classmethod
    def from_explicit_path(cls, 
            image_paths: list, label_path: str, 
            output_patch_size: Cartesian,
            num_classes: int=DEFAULT_NUM_CLASSES,
            **kwargs,
            ):
        label = load_chunk_or_volume(label_path, **kwargs)
        # print(f'label path: {label_path} with size {label.shape}')

        images = []
        for image_path in image_paths:
            image = load_chunk_or_volume(image_path, **kwargs)
            images.append(image)
            # print(f'image path: {image_path} with size {image.shape}')
        return cls(images, label, output_patch_size, num_classes=num_classes)

    @classmethod
    def from_label_path(cls, label_path: str, 
            output_patch_size: Cartesian,
            num_classes: int = DEFAULT_NUM_CLASSES):
        """construct a sample from a single file of label

        Args:
            label_path (str): the path of a label file

        Returns:
            an instance of a sample
        """
        image_path = label_path.replace('label', 'image')
        return cls.from_explicit_path(
            [image_path,], label_path, output_patch_size, num_classes=num_classes)

    @classmethod
    def from_explicit_dict(cls, d: dict, 
            output_patch_size: Cartesian,
            num_classes: int = DEFAULT_NUM_CLASSES):
        image_paths = d['images']
        label_path = d['label']
        return cls.from_explicit_path(
            image_paths, label_path, output_patch_size, num_classes=num_classes)

    @cached_property
    def voxel_num(self):
        return len(self.label)

    @cached_property
    def class_counts(self):
        return np.bincount(self.label.flatten(), minlength=self.num_classes)
    
    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            AdjustContrast(factor_range = (0.95, 1.8)),
            AdjustBrightness(min_factor = 0.05, max_factor = 0.2),
            #AdjustContrast(),
            #AdjustBrightness(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            RandomPixelDropping(),
            MaskBox(),
            # MaskBox2D(), 
            Perspective2D(),
            Rotate2D(probability=1.),
            DropSection(probability=1.),
            Flip(),
            Transpose(),
            # MissAlignment(),
            Label2AffinityMap(),  
        ])


class OrganelleSample(SemanticSample):
    def __init__(self, 
            images: List[Chunk], 
            label: Union[np.ndarray, Chunk], 
            output_patch_size: Cartesian, 
            num_classes: int = DEFAULT_NUM_CLASSES, 
            forbbiden_distance_to_boundary: tuple = None,
            skip_classes: list = None,
            selected_classes: list = None) -> None:
        super().__init__(images, label, output_patch_size, 
            num_classes=num_classes, 
            forbbiden_distance_to_boundary=forbbiden_distance_to_boundary)

        if skip_classes is not None:
            for class_idx in skip_classes:
                self.label.array[self.label.array>class_idx] -= 1
        
        if selected_classes is not None:
            self.label.array = np.isin(self.label.array, selected_classes)
    
    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            # AdjustContrast(factor_range = (0.95, 1.8)),
            # AdjustBrightness(min_factor = 0.05, max_factor = 0.2),
            AdjustContrast(),
            AdjustBrightness(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            #RandomPixelDropping(),
            MaskBox(),
            # MaskBox2D(), 
            # Perspective2D(),
            # Rotate2D(probability=1.),
            DropSection(probability=1.),
            Flip(),
            Transpose(),
            # MissAlignment(),
        ])


class AffinityMapSample(SemanticSample):
    def __init__(self, 
            images: List[Chunk], 
            label: Union[np.ndarray, Chunk], 
            output_patch_size: Cartesian, 
            forbbiden_distance_to_boundary: tuple = None,
            num_classes: int = 3) -> None:
        super().__init__(
            images, label, output_patch_size, 
            num_classes=num_classes,
            forbbiden_distance_to_boundary = forbbiden_distance_to_boundary, 
        )
        # number of classes
    
    @classmethod
    def from_explicit_path(cls, 
            image_paths: list, label_path: str, 
            output_patch_size: Cartesian,
            num_classes: int=3,
            **kwargs,
            ):
        label = load_chunk_or_volume(label_path, **kwargs)
        # print(f'label path: {label_path} with size {label.shape}')

        images = []
        for image_path in image_paths:
            image = load_chunk_or_volume(image_path, **kwargs)
            images.append(image)
            # print(f'image path: {image_path} with size {image.shape}')
        return cls(images, label, output_patch_size, num_classes=num_classes)
    
    @classmethod
    def from_explicit_dict(cls, 
            d: dict, 
            output_patch_size: Cartesian,
            num_classes: int = 3):
        image_paths = d['images']
        label_path = d['label']
        return cls.from_explicit_path(
            image_paths, label_path, output_patch_size, num_classes=num_classes)

    @classmethod
    def from_config_node(cls, 
            cfg: CfgNode,
            output_patch_size: Cartesian,
            num_classes: int=3,
            **kwargs,
        ):
        label_path = os.path.join(cfg.dir, cfg.label)
        label = load_chunk_or_volume(label_path, **kwargs)

        images = []
        for image_fname in cfg.images:
            image_path = os.path.join(cfg.dir, image_fname)
            image = load_chunk_or_volume(image_path, **kwargs)
            assert image.shape[-3:] == label.shape[-3:], \
                f'image shape: {image.shape}, label shape: {label.shape}, file name: {image_path}'
            assert image.voxel_offset == label.voxel_offset, \
                f'image voxel offset: {image.voxel_offset}, label voxel offset: {label.voxel_offset}, file name: {image_path}'
            images.append(image)

        return cls(images, label, output_patch_size, num_classes=num_classes)


    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            AdjustBrightness(),
            AdjustContrast(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            MaskBox(),
            # Perspective2D(),
            # RotateScale(probability=1.),
            # DropSection(),
            Flip(),
            Transpose(),
            MissAlignment(),
            Label2AffinityMap(probability=1.),
        ])


class AffinityMapSampleWithMask(SampleWithMask):
    def __init__(self, images: List[PrecomputedVolume], label: Union[Chunk, PrecomputedVolume], output_patch_size: Cartesian, mask: Chunk | PrecomputedVolume, forbbiden_distance_to_boundary: tuple = None, patches_in_block: int = 8, nonzero_bounding_boxes_path: str = './nonzero_bounding_boxes.npy') -> None:
        super().__init__(images, label, output_patch_size, mask, forbbiden_distance_to_boundary, patches_in_block, nonzero_bounding_boxes_path)
    
    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            AdjustBrightness(),
            AdjustContrast(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            #RandomPixelDropping(),
            MaskBox(),
            # MaskBox2D(), 
            # Perspective2D(),
            # Rotate2D(probability=1.),
            # DropSection(),
            Flip(),
            Transpose(),
            MissAlignment(),
            Label2AffinityMap(probability=1.),
        ])

class SelfSupervisedSample(Sample):
    def __init__(self, 
            images: List[Chunk], 
            label: Union[np.ndarray, Chunk], 
            output_patch_size: Cartesian, 
            forbbiden_distance_to_boundary: tuple = None) -> None:
        super().__init__(images, label, output_patch_size, forbbiden_distance_to_boundary)

    @classmethod
    def from_explicit_paths(cls, 
            image_paths: list, 
            output_patch_size: Cartesian,
            **kwargs,
            ):
        """Construct self supervised sample from a list of image paths
        Note that the first image will be used a reference or ground truth.

        Args:
            image_paths (list): _description_
            output_patch_size (Cartesian): _description_

        Returns:
            _type_: _description_
        """
        assert len(image_paths) == 1
        image = load_chunk_or_volume(image_paths[0], **kwargs)
            # print(f'image path: {image_path} with size {image.shape}')
        return cls([image], image, output_patch_size)
    
    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            AdjustContrast(),
            AdjustBrightness(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            RandomPixelDropping(), 
            MaskBox(),
            #MaskBox2D(), 
            #Rotate2D(),
            #Flip(),
            #FlipAffMap(probability=1.),
            #RotateAffMap(probability=1.),
            #Transpose(),
            Label2AffinityMap(probability=1.),
        ])


class NeuropilMaskSample(Sample):
    def __init__(self, 
            images: List[AbstractVolume], 
            label: Union[Chunk, AbstractVolume], 
            output_patch_size: Cartesian,
            mip: int = 3,
            forbbiden_distance_to_boundary: tuple = None) -> None:
        """Train a model to predict neuropil mask.
        The patch sampling is biased to neuropil mask boundary.

        Args:
            images (List[Chunk, AbstractVolume]): candidate images
            label (Union[Chunk, AbstractVolume]): neuropil mask with a lower resolution.
            output_patch_size (Cartesian): 
            forbbiden_distance_to_boundary (tuple, optional): _description_. Defaults to None.
        """
        super().__init__(images, label, output_patch_size, forbbiden_distance_to_boundary)
    
    @classmethod
    def from_explicit_path(cls, 
            image_paths: list, label_path: str, 
            output_patch_size: Cartesian,
            mip: int = 3,
            **kwargs,
            ):
        label = load_chunk_or_volume(label_path, mip = mip, **kwargs)

        images = []
        for image_path in image_paths:
            image = load_chunk_or_volume(image_path, **kwargs)
            images.append(image)
        return cls(images, label, output_patch_size)

    #@property
    #def random_patch_center(self):
    #    """biased to mask boundary"""
    
    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            AdjustBrightness(),
            AdjustContrast(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            MaskBox(),
            MaskBox2D(), 
            #Perspective2D(),
            Rotate2D(probability=1.),
            DropSection(),
            Flip(),
            FlipAffMap(), 
            Transpose(),
            MissAlignment(),
            Label2AffinityMap(probability=1.),
        ])



if __name__ == '__main__':
    import os
    from tqdm import tqdm
    from PIL import Image
    from neutorch.data.dataset import load_cfg
    
    PATCH_NUM = 100
    DEFAULT_PATCH_SIZE=Cartesian(64, 64, 64)
    OUT_DIR = os.path.expanduser('~/dropbox/patches/')
    cfg = load_cfg('./seg_affs.yaml')

    sample = SemanticSample.from_explicit_dict(
        cfg.dataset.validation.human, 
        output_patch_size=DEFAULT_PATCH_SIZE
    )
    
    for idx in tqdm(range(PATCH_NUM)):
        
        patch = sample.random_patch
        #patch = Label2AffinityMap(patch) 
        image = patch.image
        label = patch.label
        if image.shape[-3:] != DEFAULT_PATCH_SIZE.tuple:
            breakpoint()

        # section_idx = image.shape[-3]//2
        section_idx = 0
        image = image[0,0, section_idx, :,:]
        label = label[0,0, section_idx, :,:]

        image *= 255.
        im = Image.fromarray(image).convert('L')
        im.save(os.path.join(OUT_DIR, f'{idx}_image.jpg'))

        label *= 255
        lbl = Image.fromarray(label).convert('L')
        lbl.save(os.path.join(OUT_DIR, f'{idx}_label.jpg'))
