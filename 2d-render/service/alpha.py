import cv2
import torch
import numpy as np
from PIL import Image
from transformers import AutoImageProcessor, UperNetForSemanticSegmentation
from loguru import logger


class AlphaService:
	def __init__(self):
		self.processor = AutoImageProcessor.from_pretrained("upernet-convnext-tiny")
		logger.info("ALPHA INITIALIZATION 1")
		self.model = UperNetForSemanticSegmentation.from_pretrained("upernet-convnext-tiny")
		logger.info("ALPHA INITIALIZATION 2")
		self.model.eval()
		logger.info("ALPHA INITIALIZATION 3")
		self.device = torch.device("cuda")
		logger.info("ALPHA INITIALIZATION 4")
		self.model.to(self.device)
		logger.info("ALPHA INITIALIZATION COMPLETE")

	def segment_person_from_pil_image(self, frame_in):  # frame: ImageObject
		logger.info(
			"---------------------------------------------------------------------------------------------- START SEGMENTATION")
		pil_image = Image.frombytes("RGBA", (frame_in.width, frame_in.height), frame_in.data, "raw",
		                            "BGRA")
		frame = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)

		def preprocess_frame(frame):
			inputs = self.processor(images=frame,  # BGR to RGB
			                        return_tensors="pt").to(self.device)
			return inputs

		inputs = preprocess_frame(frame)

		with torch.no_grad():
			outputs = self.model(**inputs)

		logits = outputs.logits
		upsampled_logits = torch.nn.functional.interpolate(
			logits,
			size=frame.shape[:2],
			mode="bilinear",
			align_corners=False,
		)
		segmentation = upsampled_logits.argmax(dim=1)[0].cpu().numpy()

		# print("Unique segmentation values:", np.unique(segmentation))

		# ADE20K "person" class = 12
		person_class_id = 12
		mask = (segmentation == person_class_id).astype(np.uint8) * 255

		if mask.sum() == 0:
			logger.info("⚠️ No person pixels found in this image")

		# mask_colored = cv2.applyColorMap(mask, cv2.COLORMAP_INFERNO)
		# blended = cv2.addWeighted(frame, 0.7, mask_colored, 0.3, 0)

		bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
		bgra[:, :, 3] = mask
		logger.info(type(bgra))
		return bgra.tobytes()
