import torch
import numpy as np
from torch import nn, einsum
import torch.nn.functional as F

from einops import rearrange, repeat
from einops.layers.torch import Rearrange


class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)


class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, dim),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        return self.net(x)


class Attention(nn.Module):
    def __init__(self, dim, heads=8, dim_head=64, dropout=0.):
        super().__init__()
        inner_dim = dim_head * heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        self.attend = nn.Softmax(dim=-1)
        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias=False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

    def forward(self, x):
        b, n, _, h = *x.shape, self.heads
        qkv = self.to_qkv(x).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), qkv)

        dots = einsum('b h i d, b h j d -> b h i j', q, k) * self.scale

        attn = self.attend(dots)

        out = einsum('b h i j, b h j d -> b h i d', attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)


class Transformer(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, mlp_dim, dropout=0.):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim, heads=heads,
                        dim_head=dim_head, dropout=dropout)),
                PreNorm(dim, FeedForward(dim, mlp_dim, dropout=dropout))
            ]))

    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return x


class ViT(nn.Module):
    def __init__(self, *, patch_size, in_channels, out_channels, patch_emb_dim, depth, heads, mlp_dim, pos_embedding='none', channels=3, dim_head=64, dropout=0., emb_dropout=0.):
        super().__init__()

        assert pos_embedding in {
            'none', 'global', 'sin', 'CPE', 'learned'}, "pos_embedding type must be of type  'none', 'global', 'sin', 'CPE', 'learned'"

        pz, py, px = patch_size
        patch_dim_in = np.product(patch_size) * in_channels
        self.embed_patch = nn.Sequential(
            Rearrange('b n pci pz py px -> b n (pci pz py px)'),
            nn.Linear(patch_dim_in, patch_emb_dim),
        )

        self.pos_embedding = None
        if pos_embedding == '':
            pass

        self.dropout = nn.Dropout(emb_dropout)

        self.transformer = Transformer(
            patch_emb_dim, depth, heads, dim_head, mlp_dim, dropout)

        patch_dim_out = np.product(patch_size) * out_channels
        pco = out_channels
        self.unembed_patch = nn.Sequential(
            nn.Linear(patch_emb_dim, patch_dim_out),
            Rearrange('b n (pco pz py px) -> b n pco pz py px',
                      pco=pco, pz=pz, py=py, px=px),
        )

    def forward(self, img):
        x = self.embed_patch(img)
        print('post patch emb ', x.shape)

        if self.pos_embedding is not None:
            x += self.pos_embedding(x)

        x = self.dropout(x)

        x = self.transformer(x)
        print('post trans ', x.shape)
        x = self.unembed_patch(x)

        return x
