import torch
from torch import float16, bfloat16, float32
from loguru import logger
import numpy as np
from torchvision import transforms
from transformers import AutoModelForImageSegmentation
from PIL import Image
from config import Config

transform_image = transforms.Compose([
    transforms.Resize((1024, 1024)),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])


class OfflineAlphaService:
    def __init__(self):
        self.model = AutoModelForImageSegmentation.from_pretrained(f'{Config.WEIGHTS_PATH}/BiRefNet-portrait',
                                                                   trust_remote_code=True)
        self.model.eval()
        self.device = torch.device("cuda")
        self.model.to(self.device)
        self.autocast_ctx = torch.amp.autocast(device_type='cuda', dtype=[float16, bfloat16][0])
        logger.info("OFFLINE ALPHA INITIALIZATION COMPLETE")

    def segment_person_from_pil_image(self, frame_in: Image):  # frame: ImageObject
        frame = Image.frombytes("RGB", (frame_in.width, frame_in.height), frame_in.data, "raw",
                                    "BGR")
        pred_image = transform_image(frame).unsqueeze(0).to(self.device)
        with self.autocast_ctx, torch.no_grad():
            preds = self.model(pred_image)[-1].sigmoid().to(float32).cpu()
        pred = preds[0].squeeze()

        pred_resized = transforms.functional.resize(
            pred.unsqueeze(0), frame.size[::-1])[0]

        alpha_np = (pred_resized.numpy() * 255).astype(np.uint8)

        rgb_np = np.array(frame)

        rgba_np = np.dstack([rgb_np, alpha_np])
        return rgba_np.tobytes()
