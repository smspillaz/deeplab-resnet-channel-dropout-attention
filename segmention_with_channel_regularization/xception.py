import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np


def fixed_padding(inputs, kernel_size, dilation):
    """Apply padding for dilations."""
    kernel_size_effective = kernel_size + (kernel_size - 1) * (dilation - 1)
    pad_total = kernel_size_effective - 1
    pad_beg = pad_total // 2
    pad_end = pad_total - pad_beg
    padded_inputs = F.pad(inputs, (pad_beg, pad_end, pad_beg, pad_end))
    return padded_inputs


class DepthwiseSeparableConv(nn.Module):
    """Depthwise Separable Convolution.

    Basically we do a convolution over each individual channel of the
    image and stack them together to get input_channels channels back.
    Then, we "merge" the result by doing a pointwise convolution using
    a 1x1 kernel, which returns output_channels channels."""
    def __init__(self, nin, nout, kernel_size, padding, bias=False, dilation=1, stride=1):
        super(DepthwiseSeparableConv, self).__init__()
        self.depthwise = nn.Conv2d(nin,
                                   nin,
                                   kernel_size=kernel_size,
                                   padding=0,
                                   dilation=dilation,
                                   stride=stride,
                                   groups=nin,
                                   bias=bias)
        self.pointwise = nn.Conv2d(nin, nout, kernel_size=1, bias=bias)

    def forward(self, x):
        x = fixed_padding(x, self.depthwise.kernel_size[0], dilation=self.depthwise.dilation[0])
        out = self.depthwise(x)
        out = self.pointwise(out)
        return out


class Block(nn.Module):
    """Entry and Middle Block component."""

    def __init__(self, in_channels, kernel_size, middle_channels, out_channels, stride=1, maxpool_out=False, n_convs=2):
        super().__init__()

        self.skip = (
            nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=stride, padding=0)
            if in_channels != out_channels or stride != 1 else None
        )

        layers = [
            nn.ReLU(inplace=False),
            DepthwiseSeparableConv(in_channels, middle_channels, kernel_size=kernel_size, padding=1),
            nn.BatchNorm2d(middle_channels),
            nn.ReLU(inplace=False),
            DepthwiseSeparableConv(middle_channels, out_channels, kernel_size=kernel_size, padding=1),
            nn.BatchNorm2d(out_channels)
        ] + ([
            nn.ReLU(inplace=False),
            DepthwiseSeparableConv(out_channels, out_channels, kernel_size=kernel_size, padding=1),
            nn.BatchNorm2d(out_channels)
        ] if n_convs > 2 else []) + ([
            # Change from DeepLab: Instead of max pooling,
            # perform another convolution, but with the given
            # stride (this downsamples the image)
            DepthwiseSeparableConv(out_channels, out_channels, kernel_size=kernel_size, padding=1, stride=stride)
            # nn.MaxPool2d(kernel_size=kernel_size, stride=stride, padding=1)
        ] if maxpool_out else [])

        self.net = nn.Sequential(*layers)

    def forward(self, x):
        y = self.net(x)

        if self.skip:
            return y + self.skip(x)

        return y + x


class Entry(nn.Module):
    """Entry flow."""

    def __init__(self, in_channels, block_channel_pairs=None):
        super().__init__()

        block_channel_pairs = block_channel_pairs or ((64, 128), (128, 256), (256, 728))
        low_level_block_channel_pair, rest_block_channel_pairs = block_channel_pairs[0], block_channel_pairs[1:]

        self.low_level_net = nn.Sequential(
            nn.Conv2d(in_channels, 32, kernel_size=3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=False),
            nn.Conv2d(32, 64, kernel_size=3, stride=2, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=False),
            Block(in_channels=low_level_block_channel_pair[0],
                  kernel_size=3,
                  middle_channels=low_level_block_channel_pair[1],
                  out_channels=low_level_block_channel_pair[1],
                  stride=2,
                  maxpool_out=True)
        )
        self.rest_net = nn.Sequential(
            *[Block(in_channels=in_block_channels,
                    kernel_size=3,
                    middle_channels=out_block_channels,
                    out_channels=out_block_channels,
                    stride=2,
                    maxpool_out=True)
              for in_block_channels, out_block_channels in rest_block_channel_pairs]
        )

    def forward(self, x):
        low_level_feat = self.low_level_net(x)
        return self.rest_net(low_level_feat), torch.relu(low_level_feat)


class Middle(nn.Module):
    """Middle flow."""

    def __init__(self, channels, repeat):
        super().__init__()

        self.net = nn.Sequential(
            *(Block(in_channels=channels,
                    kernel_size=3,
                    middle_channels=channels,
                    out_channels=channels,
                    stride=1,
                    maxpool_out=False,
                    n_convs=3)
              for i in range(repeat))
        )

    def forward(self, x):
        return self.net(x) + x


class Exit(nn.Module):
    """Exit flow."""

    def __init__(self,
                 block_in_channels,
                 block_out_channels,
                 exit_1_channels,
                 exit_2_channels):
        super().__init__()

        self.net = nn.Sequential(
            Block(in_channels=block_in_channels,
                  kernel_size=3,
                  middle_channels=block_in_channels,
                  out_channels=block_out_channels,
                  stride=2,
                  maxpool_out=True),
            DepthwiseSeparableConv(block_out_channels, exit_1_channels, kernel_size=3, padding=1, dilation=2),
            nn.BatchNorm2d(exit_1_channels),
            nn.ReLU(inplace=False),
            # Modification to original Xception, additional block with same number of channels
            DepthwiseSeparableConv(exit_1_channels, exit_1_channels, kernel_size=3, padding=1, dilation=2),
            nn.BatchNorm2d(exit_1_channels),
            nn.ReLU(inplace=False),
            # Exit from 1536 channels to 2048 channels
            DepthwiseSeparableConv(exit_1_channels, exit_2_channels, kernel_size=3, padding=1, dilation=2),
            nn.BatchNorm2d(exit_2_channels),
            nn.ReLU(inplace=False)
        )

    def forward(self, x):
        return self.net(x)


class Xception(nn.Module):
    """Compose the entry, middle and exit layers."""

    def __init__(self, in_channels, layers, out_channels, initialization=False):
        super().__init__()

        self.entry = Entry(in_channels)
        self.net = nn.Sequential(
            Middle(channels=728, repeat=layers),
            Exit(728, 1024, 1536, out_channels),
        )
        if initialization:
            self._init_weights()

    def _init_weights(self):
        """Do weight initlaization."""
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, np.sqrt(2.0 / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def forward(self, x):
        x, low_level = self.entry(x)
        return self.net(x), low_level



class FeatureMapClassifier(nn.Module):
    """Use feature maps to classify the image."""

    def __init__(self, in_activations, out_classes):
        super().__init__()

        self.linear = nn.Linear(in_activations, out_classes)

    def forward(self, x):
        x = F.adaptive_avg_pool2d(x, (1, 1))
        x = x.view(x.size(0), -1)

        return torch.log_softmax(self.linear(x), -1)

