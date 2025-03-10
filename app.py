import os
import re
import uuid
import logging
import threading
import time
from flask import Flask, request, jsonify, send_from_directory, redirect, url_for, send_file
from flask_cors import CORS
import requests
import tempfile
from moviepy.editor import VideoFileClip, concatenate_videoclips, CompositeVideoClip, clips_array
import moviepy.video.fx.all as vfx
import shutil
from flasgger import Swagger, swag_from
import yt_dlp
import subprocess
import random
from datetime import datetime
import traceback
import sys
import math
from PIL import Image
import numpy as np
from PIL import ImageFilter
import json
import os.path
import fcntl

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Try to import selenium components, but don't fail if not available
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
    logger.info("Selenium is available for browser-based downloads")
except ImportError:
    SELENIUM_AVAILABLE = False
    logger.warning("Selenium is not available, browser-based downloads will be skipped")

# Try to import undetected-chromedriver, but don't fail if not available
try:
    import undetected_chromedriver as uc
    UC_AVAILABLE = True
    logger.info("Undetected Chromedriver is available for stealth browser downloads")
except ImportError:
    UC_AVAILABLE = False
    logger.warning("Undetected Chromedriver is not available, stealth browser downloads will be skipped")

app = Flask(__name__)
# Enable CORS for all routes with specific configurations
CORS(app, resources={r"/*": {
    "origins": "*",
    "methods": ["GET", "POST", "OPTIONS"],
    "allow_headers": ["Content-Type", "Accept", "Authorization", "X-Requested-With"],
    "expose_headers": ["Content-Length", "Content-Disposition"],
    "supports_credentials": False,
    "max_age": 86400  # 24 hours
}})

# Configure Flask to prefer HTTPS for url_for with _external=True
app.config['PREFERRED_URL_SCHEME'] = 'https'

# Add an OPTIONS method handler for all routes to support preflight requests
@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    response = jsonify({"status": "ok"})
    # Add CORS headers for preflight requests
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Accept, Authorization, X-Requested-With')
    response.headers.add('Access-Control-Max-Age', '86400')
    return response

# Configure Swagger
swagger_config = {
    "headers": [],
    "specs": [
        {
            "endpoint": "apispec",
            "route": "/apispec.json",
            "rule_filter": lambda rule: True,
            "model_filter": lambda tag: True,
        }
    ],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/docs",
    "url_prefix": "",
    "swagger_ui_bundle_js": "//unpkg.com/swagger-ui-dist@3/swagger-ui-bundle.js",
    "swagger_ui_standalone_preset_js": "//unpkg.com/swagger-ui-dist@3/swagger-ui-standalone-preset.js",
    "swagger_ui_css": "//unpkg.com/swagger-ui-dist@3/swagger-ui.css",
    "uiversion": 3,
}

swagger_template = {
    "info": {
        "title": "Video Chopper API",
        "description": "API for processing videos from Google Drive and YouTube based on timestamps",
        "contact": {
            "responsibleOrganization": "",
            "responsibleDeveloper": "",
            "email": "",
            "url": "",
        },
        "version": "1.0.0",
    },
    "schemes": ["https", "http"],
    "host": "",
    "basePath": "/",
    "tags": [
        {
            "name": "Video Processing",
            "description": "Endpoints for processing videos from different sources"
        },
        {
            "name": "Job Management",
            "description": "Endpoints for managing and checking job status"
        },
        {
            "name": "Downloads",
            "description": "Endpoints for downloading processed videos"
        }
    ],
    "definitions": {
        "ProcessRequest": {
            "type": "object",
            "properties": {
                "timestamps": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2
                    },
                    "description": "Array of [start, end] timestamp pairs in seconds"
                }
            }
        },
        "JobResponse": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "status": {"type": "string", "enum": ["queued", "processing", "completed", "failed"]},
                "message": {"type": "string"},
                "status_url": {"type": "string"}
            }
        },
        "JobStatus": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "status": {"type": "string", "enum": ["queued", "processing", "completed", "failed"]},
                "message": {"type": "string"},
                "download_url": {"type": "string"}
            }
        }
    }
}

swagger = Swagger(app, config=swagger_config, template=swagger_template)

# Directory to store processed videos
VIDEO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos")
os.makedirs(VIDEO_DIR, exist_ok=True)

# In-memory job tracking
# In a production application, this should be replaced with a persistent store
# like Redis or a database
jobs = {}

# Add after other global variables
JOBS_FILE = os.path.join(os.path.dirname(__file__), 'jobs.json')

def save_jobs():
    """Save jobs to persistent storage with proper locking and permissions."""
    global jobs
    temp_file = f"{JOBS_FILE}.tmp"
    try:
        # Ensure the directory exists with proper permissions
        os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
        
        # Write to temporary file first
        with open(temp_file, 'w') as f:
            # Acquire an exclusive lock
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(jobs, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        
        # Set permissions before rename
        os.chmod(temp_file, 0o666)
        
        # Atomic rename
        os.replace(temp_file, JOBS_FILE)
        
        # Double check permissions after rename
        os.chmod(JOBS_FILE, 0o666)
        
        # Ensure the chrome user can access it
        subprocess.run(['chown', 'chrome:chrome', JOBS_FILE], check=False)
        
        logger.info(f"Successfully saved {len(jobs)} jobs to {JOBS_FILE}")
        return True
    except Exception as e:
        logger.error(f"Error saving jobs: {str(e)}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass
        return False

def load_jobs():
    """Load jobs from persistent storage with proper locking."""
    global jobs
    try:
        if not os.path.exists(JOBS_FILE):
            logger.info(f"Jobs file {JOBS_FILE} does not exist, initializing empty jobs")
            jobs = {}
            save_jobs()  # Create the file with proper permissions
            return jobs
            
        with open(JOBS_FILE, 'r') as f:
            # Acquire a shared lock for reading
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                file_content = f.read().strip()
                if not file_content:  # Handle empty file case
                    logger.warning(f"Empty jobs file {JOBS_FILE}, initializing empty jobs")
                    jobs = {}
                else:
                    loaded_jobs = json.loads(file_content)
                    if not isinstance(loaded_jobs, dict):
                        logger.warning(f"Invalid jobs data structure in {JOBS_FILE}, initializing empty jobs")
                        loaded_jobs = {}
                    jobs = loaded_jobs
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            
        logger.info(f"Successfully loaded {len(jobs)} jobs from {JOBS_FILE}")
        return jobs
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding jobs file: {str(e)}")
        jobs = {}
        save_jobs()  # Try to save empty jobs to fix corrupted file
        return jobs
    except Exception as e:
        logger.error(f"Error loading jobs: {str(e)}")
        jobs = {}
        return jobs

def get_job_status(job_id):
    """Get job status with proper error handling and locking."""
    global jobs
    try:
        # Check if job exists in memory first
        if job_id in jobs:
            job = jobs[job_id]
            
            # Don't update last_accessed for completed jobs
            if job.get('status') != 'completed':
                job['last_accessed'] = time.time()
                save_jobs()
                
            return job
            
        # If not in memory, try to load from file
        load_jobs()
            
        if job_id not in jobs:
            logger.warning(f"Job not found: {job_id}")
            return None
            
        job = jobs[job_id]
        
        # Don't update last_accessed for completed jobs
        if job.get('status') != 'completed':
            job['last_accessed'] = time.time()
            save_jobs()
            
        return job
    except Exception as e:
        logger.error(f"Error getting job status: {str(e)}")
        return None

def update_job_status(job_id, status, message=None, download_url=None, output_file=None):
    """Update job status with proper error handling and immediate saving."""
    global jobs
    try:
        if job_id not in jobs:
            jobs[job_id] = {
                'status': status,
                'message': message or '',
                'created_at': datetime.now().isoformat(),
                'last_accessed': datetime.now().isoformat()
            }
        else:
            jobs[job_id]['status'] = status
            jobs[job_id]['last_accessed'] = datetime.now().isoformat()
            if message is not None:
                jobs[job_id]['message'] = message
                
        if download_url is not None:
            jobs[job_id]['download_url'] = download_url
        if output_file is not None:
            jobs[job_id]['output_file'] = output_file
            
        # Save immediately after updating
        if not save_jobs():
            logger.error(f"Failed to save job status update for job {job_id}")
            
        return True
    except Exception as e:
        logger.error(f"Error updating job status: {str(e)}")
        return False

# Load jobs at startup
load_jobs()

def extract_file_id(google_drive_link):
    """Extract the file ID from a Google Drive link."""
    # Pattern for different Google Drive link formats
    patterns = [
        r"https://drive\.google\.com/file/d/(.*?)(/|$|\?)",  # /file/d/ format
        r"https://drive\.google\.com/open\?id=(.*?)($|&)",   # open?id= format
        r"https://docs\.google\.com/file/d/(.*?)(/|$|\?)",   # docs format
        r"https://drive\.google\.com/drive/folders/(.*?)(/|$|\?)", # folders format
    ]
    
    for pattern in patterns:
        match = re.search(pattern, google_drive_link)
        if match:
            return match.group(1)
    
    raise ValueError("Invalid Google Drive link format")

def download_from_google_drive(url, destination):
    """Download a file from Google Drive using more reliable methods."""
    try:
        # Extract file ID from the URL
        file_id = extract_file_id(url)
        logger.info(f"Extracted file ID: {file_id}")
        
        # First attempt: Try with the direct usercontent.google.com URL (this often works for large files)
        logger.info("Trying direct usercontent.google.com download...")
        try:
            direct_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t"
            logger.info(f"Using usercontent direct URL: {direct_url}")
            
            # Use a session to maintain cookies
            session = requests.Session()
            response = session.get(direct_url, stream=True, timeout=120)
            
            # Download with progress tracking
            total_size = int(response.headers.get('content-length', 0) or 0)
            logger.info(f"Starting direct download, expected size: {total_size/1024/1024:.2f} MB")
            
            downloaded = 0
            with open(destination, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        # Log progress for large files
                        if downloaded % (10 * 1024 * 1024) == 0:  # Log every 10MB
                            logger.info(f"Downloaded {downloaded/1024/1024:.2f} MB")
            
            # Verify download was successful
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                logger.info(f"Direct usercontent download successful: {destination}, size: {os.path.getsize(destination)} bytes")
                return destination
            else:
                logger.warning("Direct usercontent download completed but file is empty or missing")
        except Exception as direct_error:
            logger.warning(f"Direct usercontent download failed: {str(direct_error)}")
        
        # Second attempt: Use yt-dlp as it's more reliable for Google Drive
        logger.info(f"Attempting download with yt-dlp: {url}")
        try:
            ydl_opts = {
                'format': 'best/bestvideo+bestaudio',
                'outtmpl': destination,
                'noplaylist': True,
                'quiet': False,
                'no_warnings': False,
                'verbose': True,  # Enable verbose output for debugging
                'retries': 10,    # Increase retry attempts
                'fragment_retries': 10,
                'skip_download': False,
                'continuedl': True  # Continue partial downloads
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
                
            # Verify download was successful
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                logger.info(f"yt-dlp download successful: {destination}, size: {os.path.getsize(destination)} bytes")
                return destination
            else:
                logger.warning("yt-dlp download completed but file is empty or missing")
        except Exception as ydl_error:
            logger.warning(f"yt-dlp download failed: {str(ydl_error)}")
        
        # Third attempt: Handle the virus scan confirmation page
        logger.info("Trying download with virus scan confirmation handling...")
        
        # Create session for maintaining cookies
        session = requests.Session()
        
        # First get the confirmation token
        confirm_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        response = session.get(confirm_url, stream=True, timeout=60)
        
        # Look for both download_warning cookie and confirmation token in HTML
        token = None
        for key, value in response.cookies.items():
            if key.startswith('download_warning'):
                token = value
                logger.info(f"Found download_warning cookie: {token}")
                break
        
        # If no cookie token found, try to find it in the HTML
        if not token and 'confirm=' in response.text:
            try:
                token = re.search(r'confirm=([0-9A-Za-z_-]+)', response.text).group(1)
                logger.info(f"Found confirmation token in HTML: {token}")
            except:
                logger.warning("Could not find confirmation token in HTML")
        
        # If we have a token, use it to confirm the download
        if token:
            # Try multiple URL formats
            urls_to_try = [
                f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}",
                f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm={token}"
            ]
            
            for dl_url in urls_to_try:
                try:
                    logger.info(f"Trying confirmed URL: {dl_url}")
                    response = session.get(dl_url, stream=True, timeout=120)
                    
                    # Check if we got actual file content rather than another confirmation page
                    if 'Content-Disposition' in response.headers:
                        # This header indicates we're getting file data
                        content_disposition = response.headers.get('Content-Disposition', '')
                        logger.info(f"Got Content-Disposition: {content_disposition}")
                        
                        # Download with progress tracking
                        total_size = int(response.headers.get('content-length', 0) or 0)
                        logger.info(f"Starting confirmed download, size: {total_size/1024/1024:.2f} MB")
                        
                        downloaded = 0
                        with open(destination, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if downloaded % (10 * 1024 * 1024) == 0:  # Log every 10MB
                                        logger.info(f"Downloaded {downloaded/1024/1024:.2f} MB")
                        
                        # Verify file exists and has content
                        if os.path.exists(destination) and os.path.getsize(destination) > 0:
                            logger.info(f"Confirmed download complete: {destination}, size: {os.path.getsize(destination)} bytes")
                            return destination
                    else:
                        logger.warning(f"Response doesn't appear to be file data for URL: {dl_url}")
                except Exception as url_error:
                    logger.warning(f"Error with URL {dl_url}: {str(url_error)}")
        
        # Fourth attempt: Using curl as a final fallback with the usercontent URL
        logger.info("Trying final fallback with curl command...")
        try:
            # Use curl command for download - often works when other methods fail
            temp_output = f"{destination}.tmp"
            curl_cmd = f"curl -L 'https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t' -o {temp_output}"
            
            # Execute curl
            logger.info(f"Executing: {curl_cmd}")
            os.system(curl_cmd)
            
            # If file exists, rename it
            if os.path.exists(temp_output) and os.path.getsize(temp_output) > 0:
                os.rename(temp_output, destination)
                logger.info(f"Curl download successful: {destination}, size: {os.path.getsize(destination)} bytes")
                return destination
            else:
                logger.error("Curl download failed or produced empty file")
                raise Exception("Curl download failed")
        except Exception as curl_error:
            logger.error(f"Curl fallback failed: {str(curl_error)}")
            raise Exception(f"All download methods failed. Final error: {str(curl_error)}")
            
    except Exception as e:
        logger.error(f"All download methods failed: {str(e)}")
        raise Exception(f"Failed to download file from Google Drive: {str(e)}")

def browser_download_youtube(url, destination, use_undetected=True):
    """
    Download YouTube videos using browser automation to bypass bot detection.
    This approach is more reliable but requires Chrome/Chromium to be installed.
    """
    logger.info(f"BROWSER METHOD: Starting browser-based download for {url}")
    
    if not SELENIUM_AVAILABLE:
        logger.error("BROWSER METHOD: Selenium not available - make sure selenium is installed")
        return None

    try:
        # Create a directory for logs and screenshots
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        
        # Save debug info
        debug_log = os.path.join(logs_dir, f'browser_download_{time.strftime("%Y%m%d_%H%M%S")}.log')
        
        with open(debug_log, 'w') as f:
            f.write(f"Starting browser download for {url}\n")
            f.write(f"Destination: {destination}\n")
            f.write(f"Selenium available: {SELENIUM_AVAILABLE}\n")
            f.write(f"Undetected ChromeDriver available: {UC_AVAILABLE}\n")
            f.write(f"DISPLAY env: {os.environ.get('DISPLAY')}\n")
            f.write(f"Chrome path: {os.environ.get('CHROME_BIN')}\n")
            
            # Check Chrome installation
            try:
                chrome_version = subprocess.check_output(['google-chrome', '--version']).decode().strip()
                f.write(f"Chrome version: {chrome_version}\n")
            except Exception as e:
                f.write(f"Error checking Chrome version: {str(e)}\n")
            
            # Check Xvfb
            try:
                xvfb_check = subprocess.check_output(['ps', 'aux']).decode()
                if 'Xvfb' in xvfb_check:
                    f.write("Xvfb is running\n")
                else:
                    f.write("WARNING: Xvfb is not running\n")
            except Exception as e:
                f.write(f"Error checking Xvfb: {str(e)}\n")
        
        # Extract video ID for direct m3u8 URL extraction
        video_id = None
        if "youtube.com/watch?v=" in url:
            video_id = url.split("youtube.com/watch?v=")[1].split("&")[0]
        elif "youtu.be/" in url:
            video_id = url.split("youtu.be/")[1].split("?")[0]
        
        if not video_id:
            logger.error("BROWSER METHOD: Could not extract video ID from URL")
            return None
            
        logger.info(f"BROWSER METHOD: Downloading YouTube video ID: {video_id}")
        
        # Find cookie files
        cookie_files = [
            '/app/cookies.txt',
            '/app/youtube_cookies.txt',
            '/app/auth/cookies.txt',
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'youtube_cookies.txt'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'auth', 'cookies.txt'),
        ]
        
        cookie_file = None
        for f in cookie_files:
            if os.path.exists(f):
                cookie_file = f
                logger.info(f"BROWSER METHOD: Found cookie file: {f}")
                break
        
        if not cookie_file:
            logger.warning("BROWSER METHOD: No cookie file found. Creating a temporary one with enhanced cookies.")
            temp_cookie_file = "/tmp/fallback_cookies.txt"
            with open(temp_cookie_file, 'w') as f:
                f.write("""# Netscape HTTP Cookie File
# http://curl.haxx.se/rfc/cookie_spec.html
# This file was generated by libcurl! Edit at your own risk.

.youtube.com	TRUE	/	TRUE	0	GPS	1
.youtube.com	TRUE	/	TRUE	0	PREF	f6=40000000&tz=Europe.Stockholm
.youtube.com	TRUE	/	TRUE	0	YSC	UghajJYlsII
.youtube.com	TRUE	/	TRUE	1750982400	CONSENT	PENDING+882
.youtube.com	TRUE	/	TRUE	1750982400	__Secure-1PSIDTS	sidts-CjEBPVxjSr2eFmZYn-QzCBwRgwUOQlPXAIJQVs1bB_52QhxeBlNhuhDFAB4NwWiUEAA
.youtube.com	TRUE	/	TRUE	1750982400	__Secure-3PSIDTS	sidts-CjEBPVxjSr2eFmZYn-QzCBwRgwUOQlPXAIJQVs1bB_52QhxeBlNhuhDFAB4NwWiUEAA
.youtube.com	TRUE	/	TRUE	1750982400	VISITOR_INFO1_LIVE	FwBxocsaDqo
.youtube.com	TRUE	/	TRUE	1750982400	__Secure-1PSID	Uwj8ZlK7wYIFdIw1tnW1EvjqAFqCEe_F6NbB1fzzIjpOtBZpBbowQQbP4T7WDeSSDqz1hA.
.youtube.com	TRUE	/	TRUE	1750982400	__Secure-3PSID	Uwj8ZlK7wYIFdIw1tnW1EvjqAFqCEe_F6NbB1fzzIjpOtBZpGctgEZPxnMZSrBBPR4yRPQ.
.youtube.com	TRUE	/	TRUE	1750982400	APISID	MNjI0aM-nnpGBZKP/ARM1M5_sTDXBA0P8i
.youtube.com	TRUE	/	TRUE	1750982400	HSID	AJVf2Da-k21KuEjZb
.youtube.com	TRUE	/	TRUE	1750982400	LOGIN_INFO	AFmmF2swRQIhAMLQUTG5KN0Yv_qFOOoM68QxkONVF7JR-MJtQJCqFRJAAiAMm-TYTHI7gXTrClg2h_kKhTcbI0YGu4U8Lc4HqN7sug:QUQ3MjNmemJYR3VveHNWcy1KSTQybUlYOWFnMWtJZ1J3QjZCeHZnUzg3NmVMRGhGQnlKUm5fS25RUlRZbGN0aHFwdURRQWJXZGtXdUNMdnFzVWJZb1ktVHd4b1MtYjNHMmR6d1ZabGc3aV9kbGxuQlZmZVlCQWV0TzNCczhUcXZMYzVkQnhBTEtNdWlUbGhJRXcxZmNZUFdzdE43amwzSFFn
.youtube.com	TRUE	/	TRUE	1750982400	SAPISID	_J2fu5CnVDwJnNjj/Aw3xHIiVYJ4ZxXAIP
.youtube.com	TRUE	/	TRUE	1750982400	SID	Uwj8ZlK7wYIFdIw1tnW1EvjqAFqCEe_F6NbB1fzzIjpOtBZpO3cHMNkgYSALCpTxH_bwJg.
.youtube.com	TRUE	/	TRUE	1750982400	SIDCC	AKEyXzWd6LTEEoG62q6l_mXcMaUYcFzrMZDQEt5e8QWyWGw3y65_mWO1YshMPMnNyTnAVDsn
.youtube.com	TRUE	/	TRUE	1750982400	SSID	AdCMxKjWkZZ3SGAWJ
.youtube.com	TRUE	/	TRUE	1750982400	__Secure-1PAPISID	_J2fu5CnVDwJnNjj/Aw3xHIiVYJ4ZxXAIP
.youtube.com	TRUE	/	TRUE	1750982400	__Secure-3PAPISID	_J2fu5CnVDwJnNjj/Aw3xHIiVYJ4ZxXAIP""")
            cookie_file = temp_cookie_file
            logger.info(f"Created temporary cookie file with enhanced cookies: {temp_cookie_file}")
        
        # Try a more direct approach first - use yt-dlp but with a special user agent and referer
        try:
            logger.info("BROWSER METHOD: First trying direct yt-dlp approach with browser emulation")
            browser_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            ytdlp_cmd = f"""yt-dlp -v --cookies={cookie_file} --user-agent="{browser_ua}" --referer="https://www.youtube.com/" --no-check-certificate -f 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best' '{url}' -o '{destination}'"""
            logger.info(f"BROWSER METHOD: Running command: {ytdlp_cmd}")
            
            proc = subprocess.run(ytdlp_cmd, shell=True, capture_output=True, text=True)
            if proc.returncode == 0 and os.path.exists(destination) and os.path.getsize(destination) > 0:
                logger.info(f"BROWSER METHOD: Direct yt-dlp approach successful! File size: {os.path.getsize(destination)}")
                return destination
            else:
                logger.warning(f"BROWSER METHOD: Direct approach failed. Return code: {proc.returncode}")
                logger.warning(f"BROWSER METHOD: Error output: {proc.stderr}")
        except Exception as e:
            logger.warning(f"BROWSER METHOD: Direct approach exception: {str(e)}")
        
        # Try undetected-chromedriver
        if UC_AVAILABLE and use_undetected:
            try:
                logger.info("BROWSER METHOD: Using undetected-chromedriver")
                
                # Setup Chrome options
                chrome_options = uc.ChromeOptions()
                chrome_options.add_argument("--headless")
                chrome_options.add_argument("--no-sandbox")
                chrome_options.add_argument("--disable-dev-shm-usage")
                chrome_options.add_argument("--disable-gpu")
                
                # Set Chrome binary path if specified in environment
                chrome_bin = os.environ.get('CHROME_BIN')
                if chrome_bin:
                    logger.info(f"BROWSER METHOD: Using Chrome binary from env: {chrome_bin}")
                    chrome_options.binary_location = chrome_bin
                
                # Initialize browser with more detailed error handling
                logger.info("BROWSER METHOD: Initializing undetected Chrome browser")
                browser = uc.Chrome(options=chrome_options)
            except Exception as e:
                logger.error(f"BROWSER METHOD: Failed to initialize Chrome browser: {str(e)}")
                raise
            
            # Use the browser to download the video
            try:
                browser.get(url)
                time.sleep(10)  # Wait for the page to load
                browser.save_screenshot(os.path.join(logs_dir, f'browser_screenshot_{video_id}.png'))
                logger.info(f"BROWSER METHOD: Screenshot saved to {os.path.join(logs_dir, f'browser_screenshot_{video_id}.png')}")
                
                # Extract video URL from the page
                video_url = browser.current_url
                logger.info(f"BROWSER METHOD: Extracted video URL: {video_url}")
                
                # Download the video
                download_from_google_drive(video_url, destination)
            except Exception as e:
                logger.error(f"BROWSER METHOD: Error downloading video: {str(e)}")
                raise
            
            # Clean up
            browser.quit()
            
            return destination
        else:
            logger.error("BROWSER METHOD: Undetected ChromeDriver not available")
            return None
    except Exception as e:
        logger.error(f"BROWSER METHOD: Error downloading video: {str(e)}")
        raise

@app.route('/process_google_drive', methods=['POST'])
@swag_from({
    'tags': ['Video Processing'],
    'summary': 'Process video from Google Drive',
    'description': 'Download and process a video from Google Drive using specified timestamps',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'google_drive_link': {
                        'type': 'string',
                        'description': 'Google Drive shareable link for the video'
                    },
                    'timestamps': {
                        'type': 'array',
                        'items': {
                            'type': 'array',
                            'items': {'type': 'number'},
                            'minItems': 2,
                            'maxItems': 2
                        },
                        'description': 'Array of [start, end] timestamp pairs in seconds'
                    }
                },
                'required': ['google_drive_link', 'timestamps']
            }
        }
    ],
    'responses': {
        '200': {
            'description': 'Job created successfully',
            'schema': {'$ref': '#/definitions/JobResponse'}
        },
        '400': {
            'description': 'Invalid request parameters'
        },
        '500': {
            'description': 'Server error'
        }
    }
})
def process_google_drive():
    try:
        data = request.get_json()
        if not data or 'google_drive_link' not in data or 'timestamps' not in data:
            return jsonify({
                'error': 'Missing required parameters: google_drive_link and timestamps required'
            }), 400

        # Generate a unique job ID
        job_id = str(uuid.uuid4())
        
        # Initialize job status
        update_job_status(job_id, 'queued', 'Job queued for processing')
        
        # Start processing in a background thread
        def process():
            # Create application context for the background thread
            with app.app_context():
                try:
                    # Create temporary directory for processing
                    with tempfile.TemporaryDirectory() as temp_dir:
                        # Download the video
                        input_path = os.path.join(temp_dir, f"input_{job_id}.mp4")
                        update_job_status(job_id, 'processing', 'Downloading video from Google Drive')
                        download_from_google_drive(data['google_drive_link'], input_path)
                        
                        # Process the video
                        update_job_status(job_id, 'processing', 'Processing video segments')
                        output_path = os.path.join(VIDEO_DIR, f"{job_id}.mp4")
                        
                        # Load the video
                        video = VideoFileClip(input_path)
                        
                        # Extract clips based on timestamps
                        clips = []
                        for start, end in data['timestamps']:
                            clip = video.subclip(start, end)
                            clips.append(clip)
                        
                        # Concatenate clips
                        final_clip = concatenate_videoclips(clips)
                        
                        # Write the output file
                        final_clip.write_videofile(output_path)
                        
                        # Clean up
                        video.close()
                        for clip in clips:
                            clip.close()
                        final_clip.close()
                        
                        # Update job status with download URL
                        download_url = url_for('download_file', filename=f"{job_id}.mp4", _external=True)
                        update_job_status(job_id, 'completed', 'Video processed successfully', 
                                       download_url=download_url, output_file=f"{job_id}.mp4")
                        
                except Exception as e:
                    logger.error(f"Error processing job {job_id}: {str(e)}")
                    update_job_status(job_id, 'failed', f"Processing failed: {str(e)}")
                
        # Start the processing thread
        thread = threading.Thread(target=process)
        thread.start()
        
        # Return job information
        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Job queued for processing',
            'status_url': url_for('get_job_status_endpoint', job_id=job_id, _external=True)
        })
        
    except Exception as e:
        logger.error(f"Error creating job: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/process_youtube', methods=['POST'])
@swag_from({
    'tags': ['Video Processing'],
    'summary': 'Process video from YouTube',
    'description': 'Download and process a video from YouTube using specified timestamps',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'youtube_url': {
                        'type': 'string',
                        'description': 'YouTube video URL'
                    },
                    'timestamps': {
                        'type': 'array',
                        'items': {
                            'type': 'array',
                            'items': {'type': 'number'},
                            'minItems': 2,
                            'maxItems': 2
                        },
                        'description': 'Array of [start, end] timestamp pairs in seconds'
                    }
                },
                'required': ['youtube_url', 'timestamps']
            }
        }
    ],
    'responses': {
        '200': {
            'description': 'Job created successfully',
            'schema': {'$ref': '#/definitions/JobResponse'}
        },
        '400': {
            'description': 'Invalid request parameters'
        },
        '500': {
            'description': 'Server error'
        }
    }
})
def process_youtube():
    try:
        data = request.get_json()
        if not data or 'youtube_url' not in data or 'timestamps' not in data:
            return jsonify({
                'error': 'Missing required parameters: youtube_url and timestamps required'
            }), 400

        # Generate a unique job ID
        job_id = str(uuid.uuid4())
        
        # Initialize job status
        update_job_status(job_id, 'queued', 'Job queued for processing')
        
        # Start processing in a background thread
        def process():
            # Create application context for the background thread
            with app.app_context():
                try:
                    # Create temporary directory for processing
                    with tempfile.TemporaryDirectory() as temp_dir:
                        # Download the video
                        input_path = os.path.join(temp_dir, f"input_{job_id}.mp4")
                        update_job_status(job_id, 'processing', 'Downloading video from YouTube')
                        browser_download_youtube(data['youtube_url'], input_path)
                        
                        # Process the video
                        update_job_status(job_id, 'processing', 'Processing video segments')
                        output_path = os.path.join(VIDEO_DIR, f"{job_id}.mp4")
                        
                        # Load the video
                        video = VideoFileClip(input_path)
                        
                        # Extract clips based on timestamps
                        clips = []
                        for start, end in data['timestamps']:
                            clip = video.subclip(start, end)
                            clips.append(clip)
                        
                        # Concatenate clips
                        final_clip = concatenate_videoclips(clips)
                        
                        # Write the output file
                        final_clip.write_videofile(output_path)
                        
                        # Clean up
                        video.close()
                        for clip in clips:
                            clip.close()
                        final_clip.close()
                        
                        # Update job status with download URL
                        download_url = url_for('download_file', filename=f"{job_id}.mp4", _external=True)
                        update_job_status(job_id, 'completed', 'Video processed successfully', 
                                       download_url=download_url, output_file=f"{job_id}.mp4")
                        
                except Exception as e:
                    logger.error(f"Error processing job {job_id}: {str(e)}")
                    update_job_status(job_id, 'failed', f"Processing failed: {str(e)}")
                
        # Start the processing thread
        thread = threading.Thread(target=process)
        thread.start()
        
        # Return job information
        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Job queued for processing',
            'status_url': url_for('get_job_status_endpoint', job_id=job_id, _external=True)
        })
        
    except Exception as e:
        logger.error(f"Error creating job: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/job/<job_id>', methods=['GET'])
@swag_from({
    'tags': ['Job Management'],
    'summary': 'Get job status',
    'description': 'Check the status of a video processing job',
    'parameters': [
        {
            'name': 'job_id',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'ID of the job to check'
        }
    ],
    'responses': {
        '200': {
            'description': 'Job status retrieved successfully',
            'schema': {'$ref': '#/definitions/JobStatus'}
        },
        '404': {
            'description': 'Job not found'
        }
    }
})
def get_job_status_endpoint(job_id):
    job = get_job_status(job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)

@app.route('/download_url/<job_id>', methods=['GET'])
@swag_from({
    'tags': ['Downloads'],
    'summary': 'Get download URL',
    'description': 'Get the download URL for a completed job',
    'parameters': [
        {
            'name': 'job_id',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'ID of the completed job'
        }
    ],
    'responses': {
        '200': {
            'description': 'Download URL retrieved successfully',
            'schema': {
                'type': 'string',
                'description': 'Direct download URL for the processed video'
            }
        },
        '202': {
            'description': 'Job is still processing'
        },
        '404': {
            'description': 'Job not found'
        },
        '500': {
            'description': 'Job failed'
        }
    }
})
def get_download_url(job_id):
    job = get_job_status(job_id)
    if job is None:
        return "Job not found", 404
    
    if job['status'] == 'completed':
        return job.get('download_url', 'Download URL not available'), 200
    elif job['status'] == 'failed':
        return f"Job failed: {job.get('message', 'Unknown error')}", 500
    else:
        return "Job is processing, please check back later", 202

@app.route('/download/<filename>', methods=['GET'])
@swag_from({
    'tags': ['Downloads'],
    'summary': 'Download processed video',
    'description': 'Download a processed video file',
    'parameters': [
        {
            'name': 'filename',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Name of the processed video file'
        }
    ],
    'responses': {
        '200': {
            'description': 'Video file download',
            'content': {
                'video/mp4': {
                    'schema': {
                        'type': 'string',
                        'format': 'binary'
                    }
                }
            }
        },
        '404': {
            'description': 'File not found'
        }
    }
})
def download_file(filename):
    try:
        return send_from_directory(VIDEO_DIR, filename, as_attachment=True)
    except Exception as e:
        return str(e), 404

@app.route('/health', methods=['GET'])
@swag_from({
    'tags': ['System'],
    'summary': 'Health check',
    'description': 'Check if the API is running',
    'responses': {
        '200': {
            'description': 'API is healthy',
            'schema': {
                'type': 'object',
                'properties': {
                    'status': {
                        'type': 'string',
                        'example': 'healthy'
                    }
                }
            }
        }
    }
})
def health_check():
    return jsonify({"status": "healthy"})
