import argparse
import os
import torch
import numpy as np
import json
import soundfile as sf

from tacotron2.hparams import get_hparams, add_hparams
from tacotron2.train import train as tacotron_train

from waveglow.train import train as waveglow_train
from waveglow.train import set_waveglow_config, set_data_config, set_dist_config
from tacotron2.text import text_to_sequence
from tacotron2.model import Tacotron2
from waveglow.glow import WaveGlow
from waveglow.denoiser import Denoiser

def remove_module_prefix(state_dict):
    """모델 state_dict에서 'module.' prefix 제거"""
    return {k.replace("module.", ""): v for k, v in state_dict.items()}


def train_tacotron(args):
    hparams = get_hparams(args, parser)

    hparams.cudnn_enabled = False
    torch.backends.cudnn.enabled = hparams.cudnn_enabled
    torch.backends.cudnn.benchmark = hparams.cudnn_benchmark
    
    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    print("Tacotron2 훈련 시작...")

    tacotron_train(args.output_directory, args.log_directory, args.checkpoint_path,
          args.warm_start, args.n_gpus, args.rank, args.group_name, hparams, device=device)

def train_waveglow(args):

    if args.config is None:
        args.config = "waveglow/config.json"

    # JSON 파일에서 설정 불러오기
    with open(args.config) as f:
        data = f.read()
    config = json.loads(data)  # JSON 파일을 Python 딕셔너리로 변환

    # 설정 값 가져오기
    train_config = config["train_config"]
    data_config = config["data_config"]
    dist_config = config["dist_config"]
    waveglow_config = config["waveglow_config"]
    
    train_config["output_directory"] = args.output_directory # Waveglow와 Tacotron2의 checkpoint 저장 디렉토리 통일
    
    set_waveglow_config(waveglow_config)
    set_data_config(data_config)
    set_dist_config(dist_config)

    num_gpus = torch.cuda.device_count()
    
    # 다중 GPU 사용 여부 체크
    if num_gpus > 1:
        if args.group_name == '':
            print("WARNING: Multiple GPUs detected but no distributed group set")
            print("Only running 1 GPU.  Use distributed.py for multiple GPUs")
            num_gpus = 1

    if num_gpus == 1 and args.rank != 0:
        raise Exception("Doing single GPU training on rank > 0")

    # cuDNN 설정 (성능 향상 옵션)
    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = False

    # waveglow_config을 따로 전달하지 않고, train.py에서 자동으로 global 변수로 사용하도록 함
    waveglow_train(num_gpus, args.rank, args.group_name, **train_config)



def load_checkpoint(checkpoint_path, model):
    """모델 체크포인트 로드"""
    assert os.path.isfile(checkpoint_path), f"Checkpoint {checkpoint_path} not found."
    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')

    # main파일로 학습된 모델 가져와 inf 할 때 적용
    if 'state_dict' in checkpoint_dict:
        model.load_state_dict(checkpoint_dict['state_dict'])
    else:
        raise KeyError(f"Invalid checkpoint format: {checkpoint_dict.keys()} (expected 'state_dict')")

    return model

class Synthesizer:
    """Tacotron2 + WaveGlow 기반 음성 합성 클래스"""
    def __init__(self, args, device):
        self.args = args
        tacotron_check = args.best_tacotron_path
        waveglow_check = args.best_waveglow__path

        self.hparams = get_hparams(args, parser)
        self.tacotron = Tacotron2(self.hparams).to(device).eval()
        self.tacotron = load_checkpoint(tacotron_check, self.tacotron)

        with open('/waveglow/config.json') as f:
            waveglow_config = json.load(f)["waveglow_config"]
    
        self.waveglow = WaveGlow(**waveglow_config).to(device).eval()
        self.waveglow = load_checkpoint(waveglow_check, self.waveglow)
        self.denoiser = Denoiser(self.waveglow)

    def inference(self, text):
        """단일 문장 음성 합성"""
        sequence = torch.IntTensor(np.array(text_to_sequence(text, ['korean_cleaners']))[None, :]).to(device)
        mel_outputs, mel_outputs_postnet, _, _ = self.tacotron.inference(sequence)
        with torch.no_grad():
            audio = self.waveglow.infer(mel_outputs_postnet, sigma=0.666)
        return audio[0].cpu().numpy(), self.hparams.sampling_rate

    def inference_phrase(self, phrase, sep_length=4000):
        """여러 문장을 처리하는 음성 합성"""
        texts = phrase.strip().split('\n')
        audios = [self.inference(text)[0] if text else np.zeros(sep_length) for text in texts]
        return np.hstack(audios), self.hparams.sampling_rate



if __name__ == '__main__':
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    parser = argparse.ArgumentParser()
    
    # ============= #
    # 실행 단계 선택 #
    # ============= #
    parser.add_argument('--mode', type=str, default='train_waveglow', 
                        choices=['train_tacotron', 'train_waveglow', 'synthesize'])
    
    # ============= #
    # 모델 학습 세팅 #
    # ============= #
    # 경로 설정
    parser.add_argument('--output_directory', default="res/checkpoints", # Best model 저장 경로
                        type=str, help='Directory to save checkpoints')
    parser.add_argument('--log_directory', default="logs",               # 학습 log 저장 경로
                        type=str, help='Directory to save logs') 
    parser.add_argument('--checkpoint_path', type=str, default=None, help='Checkpoint path for loading') # 전이학습을 위한 model 경로
        
    # Tacotron2 필수 설정 항목   
    parser.add_argument('--warm_start', action='store_true', help='Load model weights only')
    parser.add_argument('--n_gpus', type=int, default=1, help='Number of GPUs')
    
    # WaveGlow 필수 설정 항목 
    parser.add_argument('--config', type=str, help='JSON configuration file for WaveGlow training', required=False)
    
    # Tacotron2 - WaveGlow 공통 필수 설정 항목 
    parser.add_argument('--rank', type=int, default=0, help='GPU rank')
    parser.add_argument('--group_name', type=str, default='group_name', help='Distributed training group name')
    
    # ============= #
    # 음성 합성 세팅 #
    # ============= #
    # 경로 설정 
    parser.add_argument('--best_tacotron_path', default="res/checkpoints/best_tacotron2.pt",  # Best model 로드 경로
                        type=str, help='Path to load Best Tacotron2 model', required=False)
    parser.add_argument('--best_waveglow_path', default="res/checkpoints/best_waveglow.pt",   # Best model 로드 경로
                        type=str, help='Path to load Best WaveGlow model', required=False)
    parser.add_argument('--output_audio', default="res/output_audio/ex3.wav",                 # audio output 저장 경로
                        type=str, help='Path to save synthesized audio', required=False)
    # 변환 할 text 내용
    parser.add_argument('--text', type=str, default="안녕하세요.", help='Text to synthesize', required=False)

    add_hparams(parser)
    args = parser.parse_args()
    hparams = get_hparams(args, parser)
    
    
    if args.mode == 'train_tacotron':
        train_tacotron(args)

    elif args.mode == 'train_waveglow':
        train_waveglow(args)

    elif args.mode == 'synthesize':
        if not args.tacotron_checkpoint or not args.waveglow_checkpoint:
            raise ValueError("Both Tacotron2 and WaveGlow checkpoints are required for synthesis.")

        synthesizer = Synthesizer(args, device)
        audio, sampling_rate = synthesizer.inference_phrase(args.text)
        sf.write(args.output_audio, audio, sampling_rate)
        print(f"Generated audio saved to {args.output_audio}")


