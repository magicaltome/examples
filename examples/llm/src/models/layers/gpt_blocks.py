# Copyright 2022 MosaicML Examples authors
# SPDX-License-Identifier: Apache-2.0

"""GPT Blocks used for the GPT Model."""

from typing import Optional, Union

import torch
import torch.nn as nn

import examples.llm.src.models.layers.attention as attention


class GPTMLP(nn.Module):

    def __init__(self,
                 d_model: int,
                 mlp_ratio: int,
                 device: Optional[str] = None):
        super().__init__()
        self.mlp_up = nn.Linear(d_model, mlp_ratio * d_model, device=device)
        self.mlp_act = nn.GELU(approximate='none')
        self.mlp_down = nn.Linear(mlp_ratio * d_model, d_model, device=device)
        self.mlp_down._is_residual = True  # type: ignore

    def forward(self, x):
        return self.mlp_down(self.mlp_act(self.mlp_up(x)))


class GPTBlock(nn.Module):

    def __init__(self,
                 causal_attn_cls: Union[attention.FlashCausalAttention,
                                        attention.TorchCausalAttention,
                                        attention.TritonFlashCausalAttention],
                 d_model: int,
                 mlp_ratio: int,
                 alibi: bool = False,
                 resid_pdrop: float = 0.0,
                 device: Optional[str] = None,
                 **kwargs):
        super().__init__()
        if alibi:
            assert isinstance(
                causal_attn_cls,
                attention.TritonFlashCausalAttention) or isinstance(
                    causal_attn_cls, attention.TorchCausalAttention
                ), 'Only triton kernel or torch supports alibi'
        self.ln_1 = nn.LayerNorm(d_model, device=device)
        self.causal_attn = causal_attn_cls(device=device, **kwargs)
        self.ln_2 = nn.LayerNorm(d_model, device=device)
        self.mlp = GPTMLP(
            d_model=d_model,
            mlp_ratio=mlp_ratio,
            device=device,
        )
        self.resid_attn_dropout = nn.Dropout(resid_pdrop)
        self.resid_mlp_dropout = nn.Dropout(resid_pdrop)

    def forward(
        self,
        x: torch.Tensor,
        key_padding_mask: Optional[torch.ByteTensor] = None,
        attn_mask: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        a = self.ln_1(x)
        b, _ = self.causal_attn(a, key_padding_mask, attn_mask)
        x = x + self.resid_attn_dropout(b)
        m = self.ln_2(x)
        n = self.mlp(m)
        x = x + self.resid_mlp_dropout(n)
        return x
