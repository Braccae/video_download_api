import os
import platform
import shutil
import subprocess
import tempfile
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import requests
from zipfile import ZipFile
import hashlib
import time
import asyncio
import json

app = FastAPI()

class VideoURL(BaseModel):
    url: str

FFMPEG_BUILDS_URL = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest"
CONFIG_DIR = os.getenv('CONFIG', '/config')
CACHE_DIR = "/tmp/video_cache"
AUDIO_CACHE_DIR = "/tmp/audio_cache"
CACHE_EXPIRY_TIME = 15 * 60  # 15 minutes in seconds

# Ensure directories exist
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)


# Global variables to manage cache
cached_video_path = None
cached_audio_path = None
last_access_time = 0

def get_ffmpeg_url():
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "linux":
        if machine == "x86_64":
            return f"{FFMPEG_BUILDS_URL}/ffmpeg-master-latest-linux64-gpl.tar.xz"
        elif machine in ["aarch64", "arm64"]:
            return f"{FFMPEG_BUILDS_URL}/ffmpeg-master-latest-linuxarm64-gpl.tar.xz"
        else:
            raise ValueError(f"Unsupported Linux architecture: {machine}")
    else:
        raise ValueError(f"Unsupported system: {system}")

def download_and_extract_ffmpeg():
    ffmpeg_path = os.path.join(CONFIG_DIR, "ffmpeg")
    if os.path.exists(ffmpeg_path):
        return ffmpeg_path

    ffmpeg_url = get_ffmpeg_url()
    temp_dir = tempfile.mkdtemp()
    
    # Download FFmpeg
    response = requests.get(ffmpeg_url)
    archive_path = os.path.join(temp_dir, "ffmpeg.archive")
    with open(archive_path, "wb") as f:
        f.write(response.content)
    
    # Extract FFmpeg
    subprocess.run(["tar", "-xf", archive_path, "-C", temp_dir], check=True)
    
    # Find and move the ffmpeg binary
    for root, dirs, files in os.walk(temp_dir):
        if "ffmpeg" in files:
            src_path = os.path.join(root, "ffmpeg")
            shutil.move(src_path, ffmpeg_path)
            os.chmod(ffmpeg_path, 0o755)  # Ensure the binary is executable
            break
    
    # Clean up
    shutil.rmtree(temp_dir)
    
    if not os.path.exists(ffmpeg_path):
        raise FileNotFoundError("FFmpeg binary not found in the extracted files")
    
    return ffmpeg_path

def get_cache_filename(url):
    # Create a unique filename based on the URL
    return hashlib.md5(url.encode()).hexdigest() + ".mp4"

async def clean_cache():
    global cached_video_path, last_access_time
    while True:
        await asyncio.sleep(60)  # Check every minute
        if cached_video_path and time.time() - last_access_time > CACHE_EXPIRY_TIME:
            if os.path.exists(cached_video_path):
                os.remove(cached_video_path)
            cached_video_path = None
            last_access_time = 0

def get_ytdlp_config():
    config_path = os.path.join(CONFIG_DIR, "yt-dlp.json")
    if not os.path.exists(config_path):
        default_config = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4",
            "outtmpl": "%(title)s-%(id)s.%(ext)s"
        }
        with open(config_path, "w") as f:
            json.dump(default_config, f, indent=2)
    
    with open(config_path, "r") as f:
        return json.load(f)

def get_audio_cache_filename(url):
    # Create a unique filename based on the URL for audio files
    return hashlib.md5(url.encode()).hexdigest() + ".mp3"

async def clean_audio_cache():
    global cached_audio_path, last_access_time
    while True:
        await asyncio.sleep(60)  # Check every minute
        if cached_audio_path and time.time() - last_access_time > CACHE_EXPIRY_TIME:
            if os.path.exists(cached_audio_path):
                os.remove(cached_audio_path)
            cached_audio_path = None
            last_access_time = 0

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(clean_cache())
    asyncio.create_task(clean_audio_cache())

@app.post("/download/")
async def download_video(video: VideoURL, background_tasks: BackgroundTasks):
    global cached_video_path, last_access_time
    
    cache_filename = get_cache_filename(video.url)
    new_cache_filepath = os.path.join(CACHE_DIR, cache_filename)

    try:
        # Check if the requested video is the one in cache
        if cached_video_path and os.path.basename(cached_video_path) == cache_filename:
            last_access_time = time.time()
            return FileResponse(cached_video_path, media_type="video/mp4", filename=cache_filename)

        # If not, download the new video
        ffmpeg_path = download_and_extract_ffmpeg()
        
        ydl_opts = get_ytdlp_config()
        ydl_opts.update({
            'outtmpl': new_cache_filepath,
            'ffmpeg_location': os.path.dirname(ffmpeg_path)
        })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video.url])
        
        # Check if the file exists
        if not os.path.exists(new_cache_filepath):
            raise HTTPException(status_code=404, detail="Video download failed")
        
        # Remove the old cached video if it exists
        if cached_video_path and os.path.exists(cached_video_path):
            os.remove(cached_video_path)
        
        # Update cache information
        cached_video_path = new_cache_filepath
        last_access_time = time.time()
        
        # Return the video file
        return FileResponse(new_cache_filepath, media_type="video/mp4", filename=cache_filename)
    
    except Exception as e:
        # If an error occurs, remove the potentially partially downloaded file
        if os.path.exists(new_cache_filepath):
            os.remove(new_cache_filepath)
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/download-audio/")
async def download_audio(video: VideoURL, background_tasks: BackgroundTasks):
    global cached_audio_path, last_access_time
    
    cache_filename = get_audio_cache_filename(video.url)
    new_cache_filepath = os.path.join(AUDIO_CACHE_DIR, cache_filename)

    try:
        # Check if the requested audio is the one in cache
        if cached_audio_path and os.path.basename(cached_audio_path) == cache_filename:
            last_access_time = time.time()
            return FileResponse(cached_audio_path, media_type="audio/mpeg", filename=cache_filename)

        # If not, download the new audio
        ffmpeg_path = download_and_extract_ffmpeg()
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128',
            }],
            'outtmpl': new_cache_filepath[:-4],  # Remove .mp3 extension as yt-dlp will add it
            'ffmpeg_location': os.path.dirname(ffmpeg_path)
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video.url])
        
        # Check if the file exists
        if not os.path.exists(new_cache_filepath):
            raise HTTPException(status_code=404, detail="Audio download failed")
        
        # Remove the old cached audio if it exists
        if cached_audio_path and os.path.exists(cached_audio_path):
            os.remove(cached_audio_path)
        
        # Update cache information
        cached_audio_path = new_cache_filepath
        last_access_time = time.time()
        
        # Return the audio file
        return FileResponse(new_cache_filepath, media_type="audio/mpeg", filename=cache_filename)
    
    except Exception as e:
        # If an error occurs, remove the potentially partially downloaded file
        if os.path.exists(new_cache_filepath):
            os.remove(new_cache_filepath)
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("shutdown")
async def shutdown_event():
    # Clean up the cache directories on shutdown
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    shutil.rmtree(AUDIO_CACHE_DIR, ignore_errors=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)