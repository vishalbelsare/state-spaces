""" Different deep backbone that is essentially a 1-D UNet instead of ResNet/Transformer backbone.

Sequence length gets downsampled through the depth of the network while number of feature increases.
Then sequence length gets upsampled again (causally) and blocks are connected through skip connections.
"""

import math
from turtle import forward
import torch
import torch.nn as nn
import torch.nn.functional as F
from omegaconf import DictConfig
from einops import rearrange, repeat, reduce
from opt_einsum import contract

from src.models.sequence.base import SequenceModule, SequenceIdentity
from src.models.sequence.pool import DownPool, UpPool
from src.models.sequence.block import SequenceResidualBlock



class SequenceSNet(SequenceModule):
    """
    layer is a Namespace that specifies '_name_', referring to a constructor, and a list of arguments to that layer constructor. This layer must subscribe to the interface (i) takes a hidden dimension H and sequence length L (ii) forward pass transforms input sequence of shape (B, H, L) to output (B, H, L)
    """

    def __init__(
        self,
        d_model,
        n_layers,
        pool=[],
        expand=1,
        ff=2,
        prenorm=False,
        dropout=0.0,
        dropres=0.0,
        layer=None,
        residual=None,
        norm=None,
        initializer=None,
        l_max=-1,
        transposed=True,
        interp=0,
        act_pool=None,
    ):
        super().__init__()
        assert l_max > 0, "UNet must have length passed in"

        self.d_model = d_model
        H = d_model

        self.interp = interp
        if interp > 0:
            assert l_max % interp == 0, "Interpolation level must be a factor of the length"
            l_max = l_max // interp

        L = l_max
        self.L = L
        self.transposed = transposed

        # Layer arguments
        layer_cfg = layer.copy()
        layer_cfg['dropout'] = dropout
        layer_cfg['transposed'] = self.transposed
        layer_cfg['initializer'] = initializer
        layer_cfg['l_max'] = L
        print("layer config", layer_cfg)

        ff_cfg = {
            '_name_': 'ff',
            'expand': ff,
            'transposed': self.transposed,
            'activation': 'gelu',
            'initializer': initializer, # TODO
            'dropout': dropout, # TODO untie dropout
        }

        def _residual(d, i, layer):
            return SequenceResidualBlock(
                d,
                i, # temporary placeholder for i_layer
                prenorm=prenorm,
                dropout=dropres,
                layer=layer,
                residual=residual if residual is not None else 'R',
                norm=norm,
                pool=None,
            )

        # Down blocks
        d_layers = []
        for p in pool:
            # Add sequence downsampling and feature expanding
            d_layers.append(DownPool(H, H*expand, pool=p, transposed=self.transposed, activation=act_pool)) # TODO take expansion argument instead
            L //= p
            layer_cfg['l_max'] = L
            H *= expand
        self.d_layers = nn.ModuleList(d_layers)

        # Center block
        c_layers = [ ]
        for i in range(n_layers):
            c_layers.append(_residual(H, i+1, layer_cfg))
            if ff > 0: c_layers.append(_residual(H, i+1, ff_cfg))
        self.c_layers = nn.ModuleList(c_layers)

        # Up blocks
        u_layers = []
        for p in pool[::-1]:
            block = []
            H //= expand
            L *= p
            layer_cfg['l_max'] = L
            block.append(UpPool(H*expand, H, pool=p, transposed=self.transposed, activation=act_pool)) # TODO

            for i in range(n_layers):
                block.append(_residual(H, i+1, layer_cfg))
                if ff > 0: block.append(_residual(H, i+1, ff_cfg))

            u_layers.append(nn.ModuleList(block))

        self.u_layers = nn.ModuleList(u_layers)

        assert H == d_model

        self.norm = nn.LayerNorm(H)

        if interp > 0:
            interp_layers = []
            assert interp % 2 == 0
            for i in range(int(math.log2(interp))):
                block = []
                for j in range(2):
                    block.append(_residual(H, i+1, layer_cfg))
                    if ff > 0: block.append(_residual(H, i+1, ff_cfg))

                interp_layers.append(nn.ModuleList(block))

            self.interp_layers = nn.ModuleList(interp_layers)

    @property
    def d_output(self):
        return self.d_model

    def forward(self, x, state=None):
        """
        input: (batch, length, d_input)
        output: (batch, length, d_output)
        """
        if self.interp > 0:
            # Interpolation will be used to reconstruct "missing" frames
            # Subsample the input sequence and run the SNet on that
            x_all = x
            x = x[:, ::self.interp, :]

            y = torch.zeros_like(x_all)
            # Run the interpolating layers
            interp_level = self.interp
            for block in self.interp_layers:
                # Pad to the right and discard the output of the first input
                # (creates dependence on the next time step for interpolation)
                z = x_all[:, ::interp_level, :]
                if self.transposed: z = z.transpose(1, 2)
                for layer in block:
                    z, _ = layer(z)

                z = F.pad(z[:, :, 1:], (0, 1), mode='replicate')
                if self.transposed: z = z.transpose(1, 2)
                y[:, interp_level//2 - 1::interp_level, :] += z
                interp_level = int(interp_level // 2)

        if self.transposed: x = x.transpose(1, 2)

        # Down blocks
        outputs = []
        outputs.append(x)
        for layer in self.d_layers:
            x, _ = layer(x)
            outputs.append(x)

        # Center block
        for layer in self.c_layers:
            x, _ = layer(x)
        x = x + outputs.pop() # add a skip connection to the last output of the down block

        for block in self.u_layers:
            for layer in block:
                x, _ = layer(x)
                if isinstance(layer, UpPool):
                    # Before modeling layer in the block
                    x = x + outputs.pop()
                    outputs.append(x)
            x = x + outputs.pop() # add a skip connection from the input of the modeling part of this up block

        # feature projection
        if self.transposed: x = x.transpose(1, 2) # (batch, length, expand)
        x = self.norm(x)

        if self.interp > 0:
            y[:, self.interp - 1::self.interp, :] = x
            x = y

        return x, None # required to return a state

    def default_state(self, *args, **kwargs):
        """ x: (batch) """
        layers = list(self.d_layers) + list(self.c_layers) + [layer for block in self.u_layers for layer in block]
        return [layer.default_state(*args, **kwargs) for layer in layers]

    def step(self, x, state, **kwargs):
        """
        input: (batch, d_input)
        output: (batch, d_output)
        """
        # States will be popped in reverse order for convenience
        state = state[::-1]

        # Down blocks
        outputs = [] # Store all layers for SequenceUNet structure
        next_state = []
        for layer in self.d_layers:
            outputs.append(x)
            x, _next_state = layer.step(x, state=state.pop(), **kwargs)
            next_state.append(_next_state)
            if x is None: break

        # Center block
        if x is None:
            # Skip computations since we've downsized
            skipped = len(self.d_layers) - len(outputs)
            for _ in range(skipped + len(self.c_layers)):
                next_state.append(state.pop())
            for i in range(skipped):
                for _ in range(len(self.u_layers[i])):
                    next_state.append(state.pop())
            u_layers = list(self.u_layers)[skipped:]
        else:
            outputs.append(x)
            for layer in self.c_layers:
                x, _next_state = layer.step(x, state=state.pop(), **kwargs)
                next_state.append(_next_state)
            x = x + outputs.pop()
            u_layers = self.u_layers

        for block in u_layers:
            for layer in block:
                x, _next_state = layer.step(x, state=state.pop(), **kwargs)
                next_state.append(_next_state)
                if isinstance(layer, UpPool):
                    # Before modeling layer in the block
                    x = x + outputs.pop()
                    outputs.append(x)
            x = x + outputs.pop()

        # feature projection
        x = self.norm(x)
        return x, next_state

    def cache_all(self):
        modules = self.modules()
        next(modules)
        for layer in modules:
            if hasattr(layer, 'cache_all'): layer.cache_all()

def prepare_generation(model):
    model.eval()
    if hasattr(model, 'cache_all'): model.cache_all()