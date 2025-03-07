import cv2
import numpy as np
import glob
from moviepy.video.io import VideoFileClip
import os
import sys
import argparse
from tqdm import tqdm
from lama_cleaner.model_manager import ModelManager
from lama_cleaner.schema import Config, HDStrategy
import time
from datetime import timedelta
import imagehash
from PIL import Image
import numpy as np

class FrameCache:
    def __init__(self, cache_size=100, similarity_threshold=5):
        self.cache = {}
        self.cache_size = cache_size
        self.similarity_threshold = similarity_threshold
        self.access_count = {}
    
    def compute_hash(self, roi):
        small_roi = cv2.resize(roi, (64, 64))
        pil_image = Image.fromarray(cv2.cvtColor(small_roi, cv2.COLOR_BGR2RGB))
        return imagehash.phash(pil_image)
    
    def get(self, roi):
        frame_hash = self.compute_hash(roi)
        
        best_match = None
        min_distance = float('inf')
        
        for cached_hash in self.cache:
            distance = frame_hash - cached_hash
            if distance < min_distance:
                min_distance = distance
                best_match = cached_hash
        
        if best_match is not None and min_distance <= self.similarity_threshold:
            self.access_count[best_match] += 1
            return self.cache[best_match]
        
        return None
    
    def put(self, roi, processed_result):
        frame_hash = self.compute_hash(roi)
        
        if len(self.cache) >= self.cache_size:
            least_used = min(self.access_count, key=self.access_count.get)
            del self.cache[least_used]
            del self.access_count[least_used]
        
        self.cache[frame_hash] = processed_result
        self.access_count[frame_hash] = 1


class FrameSkipDetector:
    def __init__(self, keyframe_interval=5, scene_change_threshold=50.0):
        self.keyframe_interval = keyframe_interval
        self.scene_change_threshold = scene_change_threshold
        self.prev_roi = None
    
    def should_process_frame(self, frame_index, roi):
        is_keyframe = (frame_index % self.keyframe_interval == 0)
        if is_keyframe:
            return True
        
        if self.prev_roi is not None:
            diff = self.calculate_frame_difference(roi, self.prev_roi)
            if diff > self.scene_change_threshold:
                return True
        
        return False
    
    def update_prev_roi(self, roi):
        if roi is not None:
            self.prev_roi = roi.copy()
    
    @staticmethod
    def calculate_frame_difference(roi1, roi2):
        if roi1 is None or roi2 is None:
            return float('inf')
        
        gray1 = cv2.cvtColor(roi1, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(roi2, cv2.COLOR_BGR2GRAY)
        
        diff = np.mean((gray1.astype("float") - gray2.astype("float")) ** 2)
        
        return diff
    
class WatermarkDetector:
    def __init__(self, num_sample_frames=10, min_frame_count=7, dilation_kernel_size=7):
        self.num_sample_frames = num_sample_frames
        self.min_frame_count = min_frame_count
        self.dilation_kernel_size = dilation_kernel_size
        self.roi = None
    
    def get_first_valid_frame(self, video_clip, threshold=10):
        total_frames = int(video_clip.fps * video_clip.duration)
        frame_indices = [int(i * total_frames / self.num_sample_frames) for i in range(self.num_sample_frames)]

        for idx in frame_indices:
            frame = video_clip.get_frame(idx / video_clip.fps)
            if frame.mean() > threshold:
                return frame

        return video_clip.get_frame(0)
    
    def select_roi(self, video_clip):
        frame = self.get_first_valid_frame(video_clip)
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

        display_height = 720
        scale_factor = display_height / frame.shape[0]
        display_width = int(frame.shape[1] * scale_factor)
        display_frame = cv2.resize(frame, (display_width, display_height))

        instructions = "Select ROI and press SPACE or ENTER"
        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(display_frame, instructions, (10, 30), font, 1, (255, 255, 255), 2, cv2.LINE_AA)

        r = cv2.selectROI(display_frame)
        cv2.destroyAllWindows()

        self.roi = (
            int(r[0] / scale_factor), 
            int(r[1] / scale_factor), 
            int(r[2] / scale_factor), 
            int(r[3] / scale_factor)
        )
        
        return self.roi
    
    def detect_watermark_in_frame(self, frame):
        if self.roi is None:
            raise ValueError("ROI hasn't been selected yet. Call select_roi first.")
            
        roi_frame = frame[self.roi[1]:self.roi[1] + self.roi[3], self.roi[0]:self.roi[0] + self.roi[2]]
        gray_frame = cv2.cvtColor(roi_frame, cv2.COLOR_BGR2GRAY)
        _, binary_frame = cv2.threshold(gray_frame, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        mask = np.zeros_like(frame[:, :, 0], dtype=np.uint8)
        mask[self.roi[1]:self.roi[1] + self.roi[3], self.roi[0]:self.roi[0] + self.roi[2]] = binary_frame

        return mask
    
    def generate_mask(self, video_clip):
        if self.roi is None:
            self.select_roi(video_clip)
            
        total_frames = int(video_clip.duration * video_clip.fps)
        frame_indices = [int(i * total_frames / self.num_sample_frames) for i in range(self.num_sample_frames)]
        frames = [video_clip.get_frame(idx / video_clip.fps) for idx in frame_indices]
        
        masks = [self.detect_watermark_in_frame(frame) for frame in frames]
        
        final_mask = sum((mask == 255).astype(np.uint8) for mask in masks)
        final_mask = np.where(final_mask >= self.min_frame_count, 255, 0).astype(np.uint8)
        
        kernel = np.ones((self.dilation_kernel_size, self.dilation_kernel_size), np.uint8)
        dilated_mask = cv2.dilate(final_mask, kernel, iterations=2)
        
        return dilated_mask
    
    def get_roi_coordinates(self, watermark_mask, margin=50):
        y_indices, x_indices = np.where(watermark_mask > 0)
        if len(y_indices) == 0 or len(x_indices) == 0:
            raise ValueError("No watermark region found in mask")
            
        y_min = max(0, np.min(y_indices) - margin)
        y_max = min(watermark_mask.shape[0], np.max(y_indices) + margin)
        x_min = max(0, np.min(x_indices) - margin)
        x_max = min(watermark_mask.shape[1], np.max(x_indices) + margin)

        return (y_min, y_max, x_min, x_max)
    
    def extract_roi_mask(self, watermark_mask, roi_coords):
        y_min, y_max, x_min, x_max = roi_coords
        return watermark_mask[y_min:y_max, x_min:x_max]
    
    def preview_effect(self, video_clip, watermark_mask, model, config, max_height=720):
        frame = self.get_first_valid_frame(video_clip)
        
        result = lama_inpaint(frame, watermark_mask, model, config)
        
        h, w = result.shape[:2]
        
        scale_factor = max_height / h
        display_width = int(w * scale_factor)

        display_result = cv2.resize(result, (display_width, max_height))
        
        instructions = "Processed Result | Press ENTER to continue, ESC to exit"
        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(display_result, instructions, (10, 30), font, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        
        cv2.imshow("Watermark Removal Result", display_result)
        key = cv2.waitKey(0)
        cv2.destroyAllWindows()
        
        return key != 27


def get_video_info(video_clip):
    info = {
        "resolution": f"{int(video_clip.w)}x{int(video_clip.h)}",
        "duration": f"{video_clip.duration:.2f}秒",
        "total_frames": int(video_clip.duration * video_clip.fps),
        "fps": f"{video_clip.fps:.2f}",
        "format": "mp4"
    }
    return info

def initialize_lama():
    model = ModelManager(name="lama", device="cpu")
    config = Config(
        ldm_steps=25,
        hd_strategy=HDStrategy.ORIGINAL,
        hd_strategy_crop_margin=32,
        hd_strategy_crop_trigger_size=2048,
        hd_strategy_resize_limit=2048,
    )

    return model, config

def lama_inpaint(frame, mask, model, config):
    mask_binary = np.where(mask > 0, 255, 0).astype(np.uint8)
    result_rgb = model(frame, mask_binary, config)

    if result_rgb.dtype == np.float64:
        if np.max(result_rgb) <= 1.0:
            result_rgb = (result_rgb * 255).astype(np.uint8)
        else:
            result_rgb = result_rgb.astype(np.uint8)
    
    return result_rgb

def ensure_directory_exists(directory):
    if not os.path.exists(directory):
        try:
            os.makedirs(directory)
            return True
        except OSError as error:
            print(f"Error creating directory {directory}: {error}")
            return False
    
    temp_file = os.path.join(directory, f"temp_{time.time()}.tmp")
    try:
        with open(temp_file, 'w') as f:
            f.write("test")
        os.remove(temp_file)
        return True
    except Exception as e:
        print(f"No write permission in directory {directory}: {e}")
        return False

def is_valid_video_file(file):
    try:
        with VideoFileClip(file) as video_clip:
            return True
    except Exception as e:
        print(f"Invalid video file: {file}, Error: {e}")
        return False

class WatermarkProcessor:
    def __init__(self, model, config, roi_coords, roi_mask):
        self.model = model
        self.config = config
        self.roi_coords = roi_coords
        self.roi_mask = roi_mask
        self.frame_cache = FrameCache(cache_size=100, similarity_threshold=3)
        self.skip_detector = FrameSkipDetector(keyframe_interval=5, scene_change_threshold=50.0)
        self.prev_processed_roi = None
    
    def extract_roi(self, frame_bgr):
        y_min, y_max, x_min, x_max = self.roi_coords
        return frame_bgr[y_min:y_max, x_min:x_max]
    
    def process_frame(self, frame_bgr, frame_index):
        y_min, y_max, x_min, x_max = self.roi_coords
        roi = self.extract_roi(frame_bgr)
        
        process_this_frame = self.skip_detector.should_process_frame(frame_index, roi)
        
        if process_this_frame:
            processed_roi = self.frame_cache.get(roi)
            
            if processed_roi is None:
                processed_roi = lama_inpaint(roi, self.roi_mask, self.model, self.config)
                processed_roi = cv2.cvtColor(processed_roi, cv2.COLOR_BGR2RGB)
                self.frame_cache.put(roi, processed_roi)
            
            self.skip_detector.update_prev_roi(roi)
            self.prev_processed_roi = processed_roi.copy()
        else:
            processed_roi = self.prev_processed_roi
        
        blend_mask = cv2.GaussianBlur(self.roi_mask.astype(np.float32), (21, 21), 0) / 255.0
        
        result = frame_bgr.copy()
        result[y_min:y_max, x_min:x_max] = (
            blend_mask[:, :, np.newaxis] * processed_roi + 
            (1 - blend_mask[:, :, np.newaxis]) * roi
        )
        
        return result


def process_video(video_clip, output_path, watermark_mask, model, config):
    video_info = get_video_info(video_clip)
    start_time = time.time()
    
    y_indices, x_indices = np.where(watermark_mask > 0)
    if len(y_indices) == 0 or len(x_indices) == 0:
        print("No watermark region found in mask")
        return video_info
        
    margin = 50
    y_min, y_max = max(0, np.min(y_indices) - margin), min(video_clip.h, np.max(y_indices) + margin)
    x_min, x_max = max(0, np.min(x_indices) - margin), min(video_clip.w, np.max(x_indices) + margin)

    roi_coords = (y_min, y_max, x_min, x_max)
    roi_mask = watermark_mask[y_min:y_max, x_min:x_max]

    processor = WatermarkProcessor(model, config, roi_coords, roi_mask)

    frame_count = 0
    total_frames = video_info["total_frames"]
    progress_bar = tqdm(total=total_frames, desc="Processing Frames", unit="frames")
    
    def process_frame_wrapper(frame):
        nonlocal frame_count
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        result = processor.process_frame(frame_bgr, frame_count)
        
        frame_count += 1
        progress_bar.update(1)
        return cv2.cvtColor(result, cv2.COLOR_BGR2RGB)
    
    processed_video = video_clip.image_transform(lambda frame: process_frame_wrapper(frame))
    processed_video.write_videofile(f"{output_path}.mp4", codec="libx264", logger=None)
    
    progress_bar.close()
    
    end_time = time.time()
    processing_time = end_time - start_time
    processing_info = {
        "video_info": video_info,
        "processing_time": str(timedelta(seconds=int(processing_time)))
    }
    
    return processing_info

def parse_args():
    parser = argparse.ArgumentParser(description="Video Watermark Remover")
    parser.add_argument("--input", "-i", type=str, default=".", help="Input directory containing videos")
    parser.add_argument("--output", "-o", type=str, default="output", help="Output directory")
    parser.add_argument("--preview", "-p", action="store_true", help="Preview effect before processing")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    
    input_dir = args.input
    output_dir = args.output
    preview_enabled = args.preview
    
    if not ensure_directory_exists(output_dir):
        sys.exit(1)
    
    if os.path.isdir(input_dir):
        videos = [f for f in glob.glob(os.path.join(input_dir, "*")) if os.path.isfile(f) and is_valid_video_file(f)]
    else:
        print(f"Input path {input_dir} is not a directory")
        sys.exit(1)
    
    if not videos:
        print(f"No valid video files found in {input_dir}")
        sys.exit(1)
    
    watermark_detector = WatermarkDetector()
    watermark_mask = None
    
    lama_model, lama_config = initialize_lama()
    
    for video in videos:
        print(f"Processing {video}")
        video_clip = VideoFileClip(video)

        if watermark_mask is None:
            watermark_mask = watermark_detector.generate_mask(video_clip)

        if preview_enabled:
            if not watermark_detector.preview_effect(video_clip, watermark_mask, lama_model, lama_config):
                print("Processing cancelled by user")
                break

        video_name = os.path.basename(video)
        output_video_path = os.path.join(output_dir, os.path.splitext(video_name)[0])

        processing_info = process_video(video_clip, output_video_path, watermark_mask, lama_model, lama_config)
        
        print(f"Successfully processed {video_name}")
        print(f"  分辨率: {processing_info['video_info']['resolution']}")
        print(f"  时长: {processing_info['video_info']['duration']}")
        print(f"  帧率: {processing_info['video_info']['fps']}")
        print(f"  总帧数: {processing_info['video_info']['total_frames']}")
        print(f"  处理时间: {processing_info['processing_time']}")
