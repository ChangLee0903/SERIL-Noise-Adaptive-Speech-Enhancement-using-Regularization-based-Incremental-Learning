import os
from librosa import load
import numpy as np
import librosa
import scipy
from torchaudio.transforms import Spectrogram, MelScale, MFCC
from torchaudio.functional import magphase, compute_deltas
from functools import partial
import torch

hann = torch.Tensor(scipy.hanning(258)[1:-1])

def read(data, sr=16000):
    data, sr = load(data, sr=sr)
    return data / np.abs(data).max(), sr


def adnoise(speech_data, noise_data, SNR):
    noise_length = noise_data.shape[0]
    speech_length = speech_data.shape[0]

    if noise_length - speech_length <= 0:
        dup_num = np.ceil(speech_length / noise_length).astype(int)
        noise_data = np.tile(noise_data, dup_num)
        noise_length = noise_data.shape[0]

    start = np.random.randint(0, noise_length - speech_length, 1)[0]
    end = start + speech_length
    noise_data = noise_data[start:end]

    SNR_exp = 10.0**(SNR / 10.0)
    speech_var = np.dot(speech_data, speech_data)
    noise_var = np.dot(noise_data, noise_data)
    scaler = np.sqrt(speech_var / (SNR_exp * noise_var))

    return speech_data + scaler * noise_data


class OnlinePreprocessor(torch.nn.Module):
    def __init__(self, sample_rate=16000, win_len=512, hop_len=256, n_freq=257, n_mels=40, n_mfcc=13, feat_list=None, **kwargs):
        super(OnlinePreprocessor, self).__init__()
        n_fft = (n_freq - 1) * 2
        self._win_args = {'n_fft': n_fft, 'hop_length': hop, 'win_length': win}
        self.register_buffer('_window', torch.hann_window(win))
        
        self._stft_args = {'center': True, 'pad_mode': 'reflect', 'normalized': False, 'onesided': True}
        # stft_args: same default values as torchaudio.transforms.Spectrogram & librosa.core.spectrum._spectrogram
       
        self._stft = partial(torch.stft, **self._win_args, **self._stft_args)
        self._istft = partial(torchaudio.functional.istft, **self._win_args, **self._stft_args)
        self._magphase = partial(torchaudio.functional.magphase, power=2)
        
        self.feat_list = feat_list
        self.register_buffer('_pseudo_wav', torch.randn(2, 2, sample_rate))  # batch_size=2, channel_size=2
    
    def _check_list(self, feat_list):
        if feat_list is None:
            feat_list = self.feat_list
        assert type(feat_list) is list
        return feat_list

    def _transpose_list(self, feats):
        return [feat.transpose(-1, -2).contiguous() if type(feat) is torch.Tensor else feat for feat in feats]

    @classmethod
    def get_feat_config(cls, feat_type, channel=0, log=False, delta=0, cmvn=False):
        assert feat_type in ['complx', 'linear', 'phase', 'mel', 'mfcc']
        assert type(channel) is int
        assert type(log) is bool
        assert type(delta) is int and delta >= 0
        assert type(cmvn) is bool
        return {
            'feat_type': feat_type,
            'channel': channel,
            'log': log,
            'delta': delta,
            'cmvn': cmvn
        }

    def forward(self, wavs=None, feat_list=None):
        # wavs: (*, channel_size, max_len)
        # feat_list, mam_list: [{feat_type: 'mfcc', channel: 0, log: False, delta: 2, cmvn: 'True'}, ...]
        feat_list = self._check_list(feat_list)
        if wavs is None:
            max_channel_id = max([int(args['channel']) if 'channel' in args else 0 for args in feat_list])
            wavs = self._pseudo_wav.expand(-1, max_channel_id + 1, -1)
        assert wavs.dim() >= 3
        
        shape = wavs.size()
        complx = self._stft(wavs.reshape(-1, shape[-1]), window=self._window)
        complx = complx.reshape(shape[:-1] + complx.shape[-3:])
        # complx: (*, channel_size, feat_dim, max_len, 2)
        linear, phase = self._magphase(complx)
        mel = self._melscale(linear)
        mfcc = self._mfcc_trans(wavs)
        complx = complx.transpose(-1, -2).reshape(*mfcc.shape[:2], -1, mfcc.size(-1))
        # complx, linear, phase, mel, mfcc: (*, channel_size, feat_dim, max_len)

        def select_feat(variables, feat_type, channel=0, log=False, delta=0, cmvn=False):
            raw_feat = variables[feat_type].select(dim=-3, index=channel)
            # apply log scale
            if bool(log):
                raw_feat = (raw_feat + 1e-10).log()   
            feats = [raw_feat.contiguous()]
            # apply delta for features
            for _ in range(int(delta)):
                feats.append(compute_deltas(feats[-1]))
            feats = torch.cat(feats, dim=-2)
            # apply cmvn
            if bool(cmvn):
                feats = (feats - feats.mean(dim=-1, keepdim=True)) / (feats.std(dim=-1, keepdim=True) + 1e-10)
            return feats
            # return: (*, feat_dim, max_len)
        
        local_variables = locals()
        return self._transpose_list([select_feat(local_variables, **args) for args in feat_list])
        # return: [(*, max_len, feat_dim), ...]

    def istft(self, linears=None, phases=None, linear_power=2, complxs=None):
        assert complxs is not None or (linears is not None and phases is not None)
        # complxs: (*, n_freq, max_feat_len, 2) or (*, max_feat_len, n_freq * 2)
        # linears, phases: (*, max_feat_len, n_freq)

        if complxs is None:
            linears, phases = self._transpose_list([linears, phases])
            complxs = linears.pow(1/linear_power).unsqueeze(-1) * torch.stack([phases.cos(), phases.sin()], dim=-1)
        if complxs.size(-1) != 2:
            # treat complxs as: (*, max_feat_len, n_freq * 2)
            shape = complxs.size()
            complxs = complxs.view(*shape[:-1], -1, 2).transpose(-2, -3).contiguous()
        # complxs: (*, n_freq, max_feat_len, 2)
        
        return self._istft(complxs, window=self._window)
        # return: (*, max_wav_len)

    def test_istft(self, wavs=None, epsilon=1e-6):
        # wavs: (*, channel_size, max_wav_len)
        if wavs is None:
            wavs = self._pseudo_wav

        channel1, channel2 = 0, 1
        feat_list = [
            {'feat_type': 'complx', 'channel': channel1},
            {'feat_type': 'linear', 'channel': channel2},
            {'feat_type': 'phase', 'channel': channel2}
        ]
        complxs, linears, phases = self.forward(wavs, feat_list)
        assert torch.allclose(wavs.select(dim=-2, index=channel1), self.istft(complxs=complxs), atol=epsilon)
        assert torch.allclose(wavs.select(dim=-2, index=channel2), self.istft(linears=linears, phases=phases), atol=epsilon)
        print('[Test passed] stft -> istft')
