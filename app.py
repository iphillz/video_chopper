import os
import re
import uuid
import logging
import threading
import time
from flask import Flask, request, jsonify, send_from_directory, redirect, url_for
from flask_cors import CORS
import requests
import tempfile
from moviepy.editor import VideoFileClip, concatenate_videoclips
import shutil
from flasgger import Swagger, swag_from
import yt_dlp
import subprocess
import random
from datetime import datetime
import traceback

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
    "specs_route": "/docs",  # Removed trailing slash
    "url_prefix": "",  # Add empty URL prefix
    "swagger_ui_bundle_js": "//unpkg.com/swagger-ui-dist@3/swagger-ui-bundle.js",  # Use CDN
    "swagger_ui_standalone_preset_js": "//unpkg.com/swagger-ui-dist@3/swagger-ui-standalone-preset.js",  # Use CDN
    "swagger_ui_css": "//unpkg.com/swagger-ui-dist@3/swagger-ui.css",  # Use CDN
    "uiversion": 3,  # Use UI version 3
}

swagger_template = {
    "info": {
        "title": "Video Chopper API",
        "description": "API for processing videos from Google Drive based on timestamps",
        "contact": {
            "responsibleOrganization": "",
            "responsibleDeveloper": "",
            "email": "",
            "url": "",
        },
        "version": "1.0.0",
    },
    "schemes": ["https"],  # Use only HTTPS scheme
    "host": "",  # Let Swagger figure out host
    "basePath": "/",  # Set base path to root
}

swagger = Swagger(app, config=swagger_config, template=swagger_template)

# Directory to store processed videos
VIDEO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos")
os.makedirs(VIDEO_DIR, exist_ok=True)

# In-memory job tracking
# In a production application, this should be replaced with a persistent store
# like Redis or a database
jobs = {}

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
                try:
                    browser = uc.Chrome(options=chrome_options)
                    logger.info("BROWSER METHOD: Browser initialized successfully")
                except Exception as browser_init_error:
                    logger.error(f"BROWSER METHOD: Failed to initialize Chrome: {str(browser_init_error)}")
                    # Try with selenium's regular Chrome as a fallback
                    raise Exception("Chrome initialization failed")
                
                try:
                    # First load YouTube main site
                    logger.info("BROWSER METHOD: Loading YouTube main site")
                    browser.get("https://www.youtube.com")
                    time.sleep(3)  # Give more time to load
                    
                    # Take screenshot of YouTube homepage
                    screenshot_path = os.path.join(logs_dir, f"youtube_homepage_{time.strftime('%Y%m%d_%H%M%S')}.png")
                    browser.save_screenshot(screenshot_path)
                    logger.info(f"BROWSER METHOD: Saved homepage screenshot to {screenshot_path}")
                    
                    # Load cookies
                    if cookie_file:
                        logger.info(f"BROWSER METHOD: Loading cookies from {cookie_file}")
                        try:
                            with open(cookie_file, 'r') as f:
                                cookie_content = f.read()
                            
                            # Use regex to find all cookies
                            cookie_matches = re.findall(r'\.youtube\.com\s+TRUE\s+\/\s+(TRUE|FALSE)\s+\d+\s+(\S+)\s+([^\s]+)', cookie_content)
                            logger.info(f"BROWSER METHOD: Found {len(cookie_matches)} cookies")
                            
                            for http_only, name, value in cookie_matches:
                                if name and value and not name.startswith('#'):
                                    try:
                                        browser.add_cookie({
                                            'name': name,
                                            'value': value,
                                            'domain': '.youtube.com',
                                            'path': '/'
                                        })
                                    except Exception as cookie_error:
                                        logger.warning(f"BROWSER METHOD: Error adding cookie {name}: {str(cookie_error)}")
                        except Exception as e:
                            logger.warning(f"BROWSER METHOD: Error loading cookies: {str(e)}")
                    
                    # Navigate to the video page
                    logger.info(f"BROWSER METHOD: Navigating to {url}")
                    browser.get(url)
                    time.sleep(5)  # More time to load
                    
                    # Take screenshot of video page
                    screenshot_path = os.path.join(logs_dir, f"youtube_video_{time.strftime('%Y%m%d_%H%M%S')}.png")
                    browser.save_screenshot(screenshot_path)
                    logger.info(f"BROWSER METHOD: Saved video page screenshot to {screenshot_path}")
                    
                    # Save page source for debugging
                    page_source_path = os.path.join(logs_dir, f"youtube_page_source_{time.strftime('%Y%m%d_%H%M%S')}.html")
                    with open(page_source_path, 'w') as f:
                        f.write(browser.page_source)
                    logger.info(f"BROWSER METHOD: Saved page source to {page_source_path}")
                    
                    # Check for bot detection
                    if "confirm you're not a robot" in browser.page_source.lower() or "please verify" in browser.page_source.lower():
                        logger.warning("BROWSER METHOD: Bot detection triggered")
                        
                        # Try to solve the captcha
                        try:
                            # Look for recaptcha iframe
                            if "recaptcha" in browser.page_source.lower():
                                logger.info("BROWSER METHOD: Found reCAPTCHA, attempting to solve")
                                
                                iframes = browser.find_elements(By.TAG_NAME, 'iframe')
                                recaptcha_iframe = None
                                
                                for iframe in iframes:
                                    if 'recaptcha' in iframe.get_attribute('src').lower():
                                        recaptcha_iframe = iframe
                                        break
                                
                                if recaptcha_iframe:
                                    logger.info("BROWSER METHOD: Switching to reCAPTCHA iframe")
                                    browser.switch_to.frame(recaptcha_iframe)
                                    
                                    # Try to find the checkbox
                                    checkbox = browser.find_element(By.CLASS_NAME, 'recaptcha-checkbox-border')
                                    if checkbox:
                                        logger.info("BROWSER METHOD: Clicking reCAPTCHA checkbox")
                                        checkbox.click()
                                        time.sleep(2)
                                        
                                        # Take screenshot after clicking
                                        browser.switch_to.default_content()
                                        screenshot_path = os.path.join(logs_dir, "after_captcha_click.png")
                                        browser.save_screenshot(screenshot_path)
                                        logger.info(f"BROWSER METHOD: Saved post-captcha screenshot to {screenshot_path}")
                        except Exception as captcha_error:
                            logger.warning(f"BROWSER METHOD: Error handling captcha: {str(captcha_error)}")
                    
                    # Extract video URL from JavaScript
                    logger.info("BROWSER METHOD: Extracting video URL from JavaScript")
                    try:
                        # Look for ytInitialPlayerResponse
                        player_response = None
                        try:
                            # Execute JavaScript to get player response
                            player_response = browser.execute_script(
                                "return window.ytInitialPlayerResponse || "
                                "(function() { "
                                "for (const key in window) { "
                                "if (key.indexOf('ytInitialPlayerResponse') >= 0) return window[key]; "
                                "} "
                                "return null; })();"
                            )
                            
                            if player_response:
                                logger.info("BROWSER METHOD: Successfully extracted player response via JavaScript")
                                
                                # Save response for debugging
                                import json
                                response_path = os.path.join(logs_dir, "player_response.json")
                                with open(response_path, 'w') as f:
                                    json.dump(player_response, f, indent=2)
                                logger.info(f"BROWSER METHOD: Saved player response to {response_path}")
                                
                                # Get streaming URLs
                                formats = []
                                if 'streamingData' in player_response:
                                    streaming_data = player_response['streamingData']
                                    
                                    # Check for HLS manifest
                                    if 'hlsManifestUrl' in streaming_data:
                                        manifest_url = streaming_data['hlsManifestUrl']
                                        logger.info(f"BROWSER METHOD: Found HLS manifest: {manifest_url}")
                                        
                                        # Download using ffmpeg
                                        ffmpeg_cmd = f'ffmpeg -i "{manifest_url}" -c copy -bsf:a aac_adtstoasc "{destination}" -y'
                                        logger.info(f"BROWSER METHOD: Executing ffmpeg HLS download: {ffmpeg_cmd}")
                                        subprocess.run(ffmpeg_cmd, shell=True, check=True)
                                        
                                        if os.path.exists(destination) and os.path.getsize(destination) > 0:
                                            logger.info(f"BROWSER METHOD: HLS download successful - size: {os.path.getsize(destination)}")
                                            browser.quit()
                                            return destination
                                    
                                    # Check for formats directly
                                    if 'formats' in streaming_data:
                                        formats.extend(streaming_data['formats'])
                                    
                                    if 'adaptiveFormats' in streaming_data:
                                        formats.extend(streaming_data['adaptiveFormats'])
                                    
                                    if formats:
                                        logger.info(f"BROWSER METHOD: Found {len(formats)} formats")
                                        
                                        # Find highest quality video and audio
                                        video_formats = [f for f in formats if f.get('mimeType', '').startswith('video')]
                                        audio_formats = [f for f in formats if f.get('mimeType', '').startswith('audio')]
                                        
                                        video_formats.sort(key=lambda x: int(x.get('width', 0) * x.get('height', 0)), reverse=True)
                                        audio_formats.sort(key=lambda x: int(x.get('bitrate', 0)), reverse=True)
                                        
                                        if video_formats and audio_formats:
                                            video_url = video_formats[0].get('url')
                                            audio_url = audio_formats[0].get('url')
                                            
                                            if video_url and audio_url:
                                                logger.info("BROWSER METHOD: Downloading highest quality video and audio")
                                                video_temp = f"{destination}.video.mp4"
                                                audio_temp = f"{destination}.audio.m4a"
                                                
                                                # Download both
                                                subprocess.run(f'curl -s "{video_url}" -o "{video_temp}"', shell=True, check=True)
                                                subprocess.run(f'curl -s "{audio_url}" -o "{audio_temp}"', shell=True, check=True)
                                                
                                                # Combine with ffmpeg
                                                subprocess.run(f'ffmpeg -i "{video_temp}" -i "{audio_temp}" -c copy "{destination}" -y', 
                                                              shell=True, check=True)
                                                
                                                # Clean up
                                                for temp_file in [video_temp, audio_temp]:
                                                    if os.path.exists(temp_file):
                                                        os.remove(temp_file)
                                                
                                                if os.path.exists(destination) and os.path.getsize(destination) > 0:
                                                    logger.info(f"BROWSER METHOD: Combined download successful - size: {os.path.getsize(destination)}")
                                                    browser.quit()
                                                    return destination
                        except Exception as js_error:
                            logger.warning(f"BROWSER METHOD: Error extracting player response: {str(js_error)}")
                    
                        # Fallback: parse from page source
                        if not player_response:
                            logger.info("BROWSER METHOD: Trying to parse player response from page source")
                            page_source = browser.page_source
                            
                            # Look for ytInitialPlayerResponse
                            match = re.search(r'ytInitialPlayerResponse\s*=\s*({.+?});', page_source, re.DOTALL)
                            if match:
                                try:
                                    import json
                                    player_data = match.group(1)
                                    
                                    # Try to clean up the JS before parsing as JSON
                                    player_data = re.sub(r'function\s*\([^)]*\)\s*{[^}]*}', '{}', player_data)
                                    player_data = re.sub(r'new [a-zA-Z]+\([^)]*\)', '{}', player_data)
                                    # Convert JS property names to JSON format
                                    player_data = re.sub(r'([{,])\s*(\w+):', r'\1"\2":', player_data)
                                    
                                    # Try to parse JSON
                                    try:
                                        player_response = json.loads(player_data)
                                        logger.info("BROWSER METHOD: Successfully parsed player response from page source")
                                    except json.JSONDecodeError:
                                        logger.warning("BROWSER METHOD: Could not parse full JSON, trying partial extraction")
                                        
                                        # Try to extract just the streaming data
                                        streaming_data_match = re.search(r'"streamingData"\s*:\s*({.+?}),\s*"playbackTracking"', player_data)
                                        if streaming_data_match:
                                            streaming_data = streaming_data_match.group(1)
                                            
                                            # Extract adaptive formats array
                                            formats_match = re.search(r'"adaptiveFormats"\s*:\s*(\[.+?\])', streaming_data)
                                            if formats_match:
                                                formats_json = formats_match.group(1)
                                                formats_json = re.sub(r'([{,])\s*(\w+):', r'\1"\2":', formats_json)
                                                
                                                try:
                                                    formats = json.loads(formats_json)
                                                    logger.info(f"BROWSER METHOD: Extracted {len(formats)} formats directly")
                                                    
                                                    # Find URLs
                                                    for fmt in formats:
                                                        if "url" in fmt:
                                                            video_url = fmt.get("url")
                                                            if "video" in fmt.get("mimeType", ""):
                                                                logger.info("BROWSER METHOD: Found direct video URL")
                                                                video_temp = f"{destination}.direct.mp4"
                                                                
                                                                # Try to download directly
                                                                subprocess.run(f'curl -s "{video_url}" -o "{video_temp}"', shell=True, check=True)
                                                                
                                                                if os.path.exists(video_temp) and os.path.getsize(video_temp) > 0:
                                                                    os.rename(video_temp, destination)
                                                                    logger.info(f"BROWSER METHOD: Direct URL download successful - size: {os.path.getsize(destination)}")
                                                                    browser.quit()
                                                                    return destination
                                                except Exception as fmt_error:
                                                    logger.warning(f"BROWSER METHOD: Error parsing formats: {str(fmt_error)}")
                                except Exception as parse_error:
                                    logger.warning(f"BROWSER METHOD: Error parsing player response: {str(parse_error)}")
                    except Exception as extract_error:
                        logger.warning(f"BROWSER METHOD: Error extracting video URL: {str(extract_error)}")
                    
                    # Try using browser cookies with yt-dlp as last resort
                    logger.info("BROWSER METHOD: Extracting browser cookies for yt-dlp")
                    try:
                        # Get cookies from browser
                        browser_cookies = browser.get_cookies()
                        browser_cookie_file = f"{destination}.browser_cookies.txt"
                        
                        with open(browser_cookie_file, 'w') as f:
                            f.write("# Netscape HTTP Cookie File\n")
                            for cookie in browser_cookies:
                                domain = cookie['domain'] if cookie['domain'].startswith('.') else f".{cookie['domain']}"
                                path = cookie.get('path', '/')
                                secure = "TRUE" if cookie.get('secure', False) else "FALSE"
                                http_only = "TRUE" if cookie.get('httpOnly', False) else "FALSE"
                                expires = str(int(cookie.get('expiry', int(time.time() + 3600))))
                                name = cookie['name']
                                value = cookie['value']
                                
                                f.write(f"{domain}\t{http_only}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
                        
                        # Use yt-dlp with browser cookies
                        ytdlp_cmd = f"yt-dlp -v --cookies={browser_cookie_file} --referer='https://www.youtube.com/' --user-agent='{browser.execute_script('return navigator.userAgent')}' --no-check-certificate -f 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best' '{url}' -o '{destination}'"
                        logger.info(f"BROWSER METHOD: Running yt-dlp with browser cookies: {ytdlp_cmd}")
                        
                        subprocess.run(ytdlp_cmd, shell=True, check=True)
                        
                        if os.path.exists(destination) and os.path.getsize(destination) > 0:
                            logger.info(f"BROWSER METHOD: yt-dlp with browser cookies successful - size: {os.path.getsize(destination)}")
                            browser.quit()
                            return destination
                    except Exception as cookie_error:
                        logger.warning(f"BROWSER METHOD: Error using browser cookies: {str(cookie_error)}")
                    
                except Exception as browser_error:
                    logger.error(f"BROWSER METHOD: Browser automation error: {str(browser_error)}")
                
                # Ensure browser is closed
                try:
                    browser.quit()
                except Exception:
                    pass
                    
            except Exception as uc_error:
                logger.error(f"BROWSER METHOD: undetected-chromedriver error: {str(uc_error)}")
        
        # Fall back to regular Selenium if needed
        if SELENIUM_AVAILABLE and (not UC_AVAILABLE or not use_undetected):
            try:
                logger.info("BROWSER METHOD: Falling back to regular Selenium")
                
                # Setup Chrome options
                chrome_options = Options()
                chrome_options.add_argument("--headless")
                chrome_options.add_argument("--no-sandbox")
                chrome_options.add_argument("--disable-dev-shm-usage")
                chrome_options.add_argument("--disable-gpu")
                chrome_options.add_argument("--disable-blink-features=AutomationControlled")
                chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
                
                # Set Chrome binary path if specified in environment
                chrome_bin = os.environ.get('CHROME_BIN')
                if chrome_bin:
                    logger.info(f"BROWSER METHOD: Using Chrome binary from env for Selenium: {chrome_bin}")
                    chrome_options.binary_location = chrome_bin
                
                # Initialize the service
                service = Service(os.environ.get('CHROMEDRIVER_PATH', ChromeDriverManager().install()))
                
                # Initialize browser
                browser = webdriver.Chrome(service=service, options=chrome_options)
                
                try:
                    # Hide webdriver usage
                    browser.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                    
                    # Similar automation steps as with undetected-chromedriver
                    # ... (would be identical to the procedure above)
                    
                finally:
                    browser.quit()
                
            except Exception as selenium_error:
                logger.error(f"BROWSER METHOD: Regular Selenium error: {str(selenium_error)}")
        
        # If all browser-based methods failed, try running youtube-dl directly with mpv's user agent
        try:
            logger.info("BROWSER METHOD: Last resort - Using mpv's user agent with yt-dlp")
            mpv_ua = "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/118.0"
            
            ytdlp_mpv_cmd = f"""yt-dlp -v --cookies={cookie_file} --user-agent="{mpv_ua}" --referer="https://www.youtube.com/" -f 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best[height<=720]' '{url}' -o '{destination}'"""
            logger.info(f"BROWSER METHOD: Running mpv command: {ytdlp_mpv_cmd}")
            
            proc = subprocess.run(ytdlp_mpv_cmd, shell=True, capture_output=True, text=True)
            if proc.returncode == 0 and os.path.exists(destination) and os.path.getsize(destination) > 0:
                logger.info(f"BROWSER METHOD: mpv approach successful! File size: {os.path.getsize(destination)}")
                return destination
            else:
                logger.warning(f"BROWSER METHOD: mpv approach failed. Return code: {proc.returncode}")
        except Exception as e:
            logger.warning(f"BROWSER METHOD: mpv approach exception: {str(e)}")
        
        logger.error("BROWSER METHOD: All browser-based download methods failed")
        return None
    
    except Exception as e:
        logger.error(f"BROWSER METHOD: Unhandled exception: {str(e)}")
        import traceback
        logger.error(f"BROWSER METHOD: Traceback: {traceback.format_exc()}")
    
    return None

def download_from_youtube(url, destination=None):
    """
    Download a video from YouTube.
    
    Args:
        url (str): The URL of the YouTube video.
        destination (str, optional): The path where the video should be saved.
            If not provided, a default path will be generated.
        
    Returns:
        str: The path to the downloaded file.
    """
    logger.info(f"Starting download from YouTube URL: {url}")
    
    # Create videos directory if it doesn't exist
    videos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'videos')
    os.makedirs(videos_dir, exist_ok=True)
    
    # Extract video ID from URL for better file naming
    video_id = None
    try:
        if "youtube.com/watch?v=" in url:
            video_id = url.split("youtube.com/watch?v=")[1].split("&")[0]
        elif "youtu.be/" in url:
            video_id = url.split("youtu.be/")[1].split("?")[0]
        
        logger.info(f"Extracted video ID: {video_id}")
    except Exception as e:
        logger.warning(f"Error extracting video ID: {str(e)}")
        video_id = None
    
    # If destination is not provided, generate a default one
    if not destination:
        # Generate a unique filename
        timestamp = int(time.time())
        filename = f"{video_id}_{timestamp}.mp4" if video_id else f"youtube_{timestamp}.mp4"
        destination = os.path.join(videos_dir, filename)
    
    logger.info(f"Will download to: {destination}")
    
    # Multiple user agents to rotate and try
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 OPR/110.0.0.0",
        "Mozilla/5.0 (iPad; CPU OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Vivaldi/6.6.3271.50"
    ]
    
    # Check environment for debugging
    logger.info(f"Current working directory: {os.getcwd()}")
    logger.info(f"Files in current directory: {os.listdir('.')}")
    if os.path.exists('/app'):
        logger.info(f"Files in /app: {os.listdir('/app')}")
    
    # First, try the browser-based method which has the best chance of bypassing bot detection
    try:
        logger.info("Attempting browser-based download first")
        browser_result = browser_download_youtube(url, destination)
        
        if browser_result and os.path.exists(browser_result) and os.path.getsize(browser_result) > 0:
            logger.info(f"Browser-based download successful: {os.path.getsize(browser_result)} bytes")
            return browser_result
        else:
            logger.warning("Browser-based download failed, falling back to yt-dlp")
    except Exception as browser_err:
        logger.error(f"Browser download error: {str(browser_err)}")
        logger.info("Falling back to yt-dlp methods")
    
    # If browser method fails, try all the fallback methods
    try:
        # Find cookie files or generate dynamic ones
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
                logger.info(f"Found cookie file: {f}")
                with open(f, 'r') as cf:
                    cookie_sample = cf.read(100)  # Read first 100 chars to verify it's not empty
                    logger.info(f"Cookie file sample: {cookie_sample}...")
                break
        
        if not cookie_file:
            logger.info("No cookie file found. Generating enhanced YouTube cookies.")
            cookie_file = generate_enhanced_youtube_cookies()
        
        # Try different download methods in sequence with multiple user agents
        success = False
        
        for user_agent in user_agents:
            logger.info(f"Trying with user agent: {user_agent[:30]}...")
            
            # Method 1: Direct command execution with yt-dlp (with cookies)
            try:
                logger.info("Trying yt-dlp direct command execution with cookies")
                command = f'yt-dlp -v --cookies={cookie_file} --no-check-certificate --user-agent="{user_agent}" ' \
                          f'--referer="https://www.youtube.com/" ' \
                          f'--sleep-interval 2 --sleep-interval-requests 1 ' \
                          f'--retries 15 --fragment-retries 15 ' \
                          f'--geo-bypass --geo-bypass-country US ' \
                          f'--socket-timeout 60 ' \
                          f'--concurrent-fragments 1 ' \
                          f'-f "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]" ' \
                          f'"{url}" -o "{destination}"'
                logger.info(f"Running command: {command}")
                
                result = subprocess.run(command, shell=True, capture_output=True, text=True)
                logger.info(f"Command exit code: {result.returncode}")
                
                if result.returncode == 0 and os.path.exists(destination) and os.path.getsize(destination) > 0:
                    logger.info(f"Direct command successful: {os.path.getsize(destination)} bytes")
                    success = True
                    return destination
                else:
                    logger.warning(f"Direct command failed: {result.stderr}")
            except Exception as cmd_err:
                logger.warning(f"Direct command error: {str(cmd_err)}")
            
            # Method 2: yt-dlp API with this user agent
            try:
                logger.info(f"Trying yt-dlp API with user agent: {user_agent[:30]}...")
                ydl_opts = {
                    'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]',
                    'outtmpl': destination,
                    'cookiefile': cookie_file,
                    'verbose': True,
                    'no_warnings': False,
                    'noplaylist': True,
                    'retries': 15,                # Increased retries
                    'fragment_retries': 15,       # Increased fragment retries
                    'skip_unavailable_fragments': True,
                    'user_agent': user_agent,
                    'referer': 'https://www.youtube.com/',
                    'sleep_interval_requests': 1, # Add delay between requests
                    'sleep_interval': 2,          # Add delay between fragments
                    'geo_bypass': True,           # Try to bypass geo-restriction
                    'geo_bypass_country': 'US',   # Use US as fallback country
                    'extractor_retries': 5,       # Retry extraction
                    'socket_timeout': 60,         # Increase socket timeout
                    'force_generic_extractor': False,
                    'concurrent_fragment_downloads': 1  # Less aggressive downloading
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    logger.info("Downloading with yt-dlp...")
                    ydl.download([url])
                
                if os.path.exists(destination) and os.path.getsize(destination) > 0:
                    logger.info(f"yt-dlp API successful: {os.path.getsize(destination)} bytes")
                    success = True
                    return destination
                else:
                    logger.warning("yt-dlp API output file not found or empty")
            except Exception as ydl_err:
                logger.warning(f"yt-dlp error: {str(ydl_err)}")
        
        # Method 4: Try invidious as an alternative YouTube frontend
        if not success and video_id:
            try:
                logger.info("Trying Invidious API as alternative YouTube frontend")
                invidious_instances = [
                    "https://invidious.snopyta.org",
                    "https://yewtu.be",
                    "https://invidious.kavin.rocks",
                    "https://inv.riverside.rocks"
                ]
                
                for instance in invidious_instances:
                    try:
                        logger.info(f"Trying Invidious instance: {instance}")
                        api_url = f"{instance}/api/v1/videos/{video_id}"
                        headers = {"User-Agent": random.choice(user_agents)}
                        
                        response = requests.get(api_url, headers=headers, timeout=10)
                        if response.status_code == 200:
                            video_data = response.json()
                            
                            if "adaptiveFormats" in video_data:
                                formats = video_data["adaptiveFormats"]
                                # Sort by quality (height)
                                video_formats = [f for f in formats if "video" in f.get("type", "")]
                                video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)
                                
                                if video_formats:
                                    format_url = video_formats[0]["url"]
                                    logger.info(f"Found direct URL via Invidious: {format_url[:50]}...")
                                    
                                    # Download with requests
                                    with requests.get(format_url, stream=True) as r:
                                        r.raise_for_status()
                                        with open(destination, 'wb') as f:
                                            for chunk in r.iter_content(chunk_size=8192):
                                                f.write(chunk)
                                    
                                    if os.path.exists(destination) and os.path.getsize(destination) > 0:
                                        logger.info(f"Invidious download successful: {os.path.getsize(destination)} bytes")
                                        success = True
                                        return destination
                    except Exception as inv_err:
                        logger.warning(f"Invidious instance {instance} error: {str(inv_err)}")
            except Exception as invidious_err:
                logger.warning(f"Invidious API error: {str(invidious_err)}")
        
        # Method 5: Try with youtube-dl as a fallback
        if not success:
            try:
                logger.info("Trying youtube-dl as fallback")
                import youtube_dl
                
                for user_agent in user_agents:
                    youtubedl_opts = {
                        'format': 'best[height<=720]',
                        'outtmpl': destination,
                        'cookiefile': cookie_file,
                        'quiet': False,
                        'no_warnings': False,
                        'user_agent': user_agent
                    }
                    
                    with youtube_dl.YoutubeDL(youtubedl_opts) as ydl:
                        logger.info(f"Downloading with youtube-dl using agent: {user_agent[:30]}...")
                        ydl.download([url])
                    
                    if os.path.exists(destination) and os.path.getsize(destination) > 0:
                        logger.info(f"youtube-dl successful: {os.path.getsize(destination)} bytes")
                        success = True
                        return destination
                    
                    logger.warning("youtube-dl attempt failed, trying next user agent")
            except Exception as ytd_err:
                logger.warning(f"youtube-dl error: {str(ytd_err)}")
        
        # Last resort - try with lower quality as it might bypass some restrictions
        if not success:
            try:
                logger.info("Trying lower quality download as ultimate fallback")
                ydl_opts = {
                    'format': 'worst',  # Use lowest quality as a fallback
                    'outtmpl': destination,
                    'verbose': True,
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    logger.info("Downloading lowest quality with yt-dlp...")
                    ydl.download([url])
                
                if os.path.exists(destination) and os.path.getsize(destination) > 0:
                    logger.info(f"Low quality download successful: {os.path.getsize(destination)} bytes")
                    return destination
                else:
                    logger.warning("Low quality download file not found or empty")
            except Exception as low_err:
                logger.warning(f"Low quality download error: {str(low_err)}")
        
        # If we get here, all methods have failed
        logger.error("All download methods failed")
        raise Exception("Failed to download video from YouTube - bot detection or restricted content")
            
    except Exception as e:
        logger.error(f"YouTube download error: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise

def download_from_youtube_proxy(url, destination=None):
    """
    Download a video from YouTube using proxy services that are less likely to be blocked.
    
    Args:
        url (str): The URL of the YouTube video.
        destination (str, optional): The path where the video should be saved.
            If not provided, a default path will be generated.
        
    Returns:
        str: The path to the downloaded file.
    """
    logger.info(f"Starting proxy download from YouTube URL: {url}")
    
    # Create videos directory if it doesn't exist
    videos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'videos')
    os.makedirs(videos_dir, exist_ok=True)
    
    # Extract video ID from URL for better file naming
    video_id = None
    try:
        if "youtube.com/watch?v=" in url:
            video_id = url.split("youtube.com/watch?v=")[1].split("&")[0]
        elif "youtu.be/" in url:
            video_id = url.split("youtu.be/")[1].split("?")[0]
        
        logger.info(f"Extracted video ID: {video_id}")
    except Exception as e:
        logger.warning(f"Error extracting video ID: {str(e)}")
        video_id = None
    
    # If destination is not provided, generate a default one
    if not destination:
        # Generate a unique filename
        timestamp = int(time.time())
        filename = f"{video_id}_{timestamp}.mp4" if video_id else f"youtube_{timestamp}.mp4"
        destination = os.path.join(videos_dir, filename)
    
    logger.info(f"Will download to: {destination}")
    
    # List of proxy services to try
    proxy_services = [
        # piped.video API - open source YouTube alternative
        {
            "name": "Piped",
            "base_url": "https://pipedapi.kavin.rocks",
            "url_format": "https://pipedapi.kavin.rocks/streams/{video_id}",
            "download_method": "piped"
        },
        # Invidious API - another YouTube alternative
        {
            "name": "Invidious",
            "base_url": "https://invidious.snopyta.org",
            "url_format": "https://invidious.snopyta.org/api/v1/videos/{video_id}",
            "download_method": "invidious"
        },
        # Alternative Invidious instance
        {
            "name": "Invidious Alternative",
            "base_url": "https://yewtu.be",
            "url_format": "https://yewtu.be/api/v1/videos/{video_id}",
            "download_method": "invidious"
        },
        # youtube-dl-server API if set up
        {
            "name": "YoutubeDL Server",
            "base_url": os.environ.get("YOUTUBE_DL_SERVER", ""),
            "url_format": "{base_url}/api/info?url=https://www.youtube.com/watch?v={video_id}&flatten=True",
            "download_method": "youtube_dl_server"
        }
    ]
    
    success = False
    error_messages = []
    
    # Try each proxy service until one works
    for service in proxy_services:
        if not video_id and service["download_method"] in ["piped", "invidious", "youtube_dl_server"]:
            logger.warning(f"Skipping {service['name']} as video_id could not be extracted")
            continue
            
        if service["download_method"] == "youtube_dl_server" and not service["base_url"]:
            logger.warning("Skipping YoutubeDL Server as base URL is not set")
            continue
        
        logger.info(f"Trying to download using {service['name']} proxy service")
        
        try:
            if service["download_method"] == "piped":
                # Piped API approach
                api_url = service["url_format"].format(video_id=video_id)
                logger.info(f"Requesting stream info from Piped API: {api_url}")
                
                response = requests.get(api_url, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    
                    # Look for video streams
                    if "videoStreams" in data and data["videoStreams"]:
                        # Sort by quality (height)
                        video_streams = sorted(
                            data["videoStreams"], 
                            key=lambda x: int(x.get("height", 0)), 
                            reverse=True
                        )
                        
                        # Get highest quality with reasonable size
                        selected_stream = None
                        for stream in video_streams:
                            if stream.get("height", 0) <= 720:  # Limit to 720p to avoid getting blocked
                                selected_stream = stream
                                break
                        
                        if not selected_stream and video_streams:
                            selected_stream = video_streams[-1]  # Use lowest quality if no 720p
                        
                        if selected_stream and "url" in selected_stream:
                            video_url = selected_stream["url"]
                            logger.info(f"Found video URL via Piped API: {video_url[:50]}...")
                            
                            # Download video
                            with requests.get(video_url, stream=True) as r:
                                r.raise_for_status()
                                with open(destination, 'wb') as f:
                                    for chunk in r.iter_content(chunk_size=8192):
                                        f.write(chunk)
                            
                            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                                logger.info(f"Piped API download successful: {os.path.getsize(destination)} bytes")
                                success = True
                                return destination
                    
                    # If video-only, look for audio streams to download separately
                    elif "audioStreams" in data and data["audioStreams"]:
                        logger.info("No video streams found. Trying audio-only format.")
                        audio_streams = sorted(
                            data["audioStreams"], 
                            key=lambda x: int(x.get("bitrate", 0)), 
                            reverse=True
                        )
                        
                        if audio_streams and "url" in audio_streams[0]:
                            audio_url = audio_streams[0]["url"]
                            logger.info(f"Found audio URL via Piped API: {audio_url[:50]}...")
                            
                            # Download audio
                            with requests.get(audio_url, stream=True) as r:
                                r.raise_for_status()
                                with open(destination, 'wb') as f:
                                    for chunk in r.iter_content(chunk_size=8192):
                                        f.write(chunk)
                            
                            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                                logger.info(f"Piped API audio download successful: {os.path.getsize(destination)} bytes")
                                success = True
                                return destination
                
            elif service["download_method"] == "invidious":
                # Invidious API approach
                api_url = service["url_format"].format(video_id=video_id)
                logger.info(f"Requesting video info from Invidious API: {api_url}")
                
                response = requests.get(api_url, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    
                    if "adaptiveFormats" in data:
                        formats = data["adaptiveFormats"]
                        
                        # Get video formats and sort by quality
                        video_formats = [f for f in formats if "video" in f.get("type", "")]
                        video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)
                        
                        selected_format = None
                        for video_format in video_formats:
                            if video_format.get("height", 0) <= 720:  # Limit to 720p
                                selected_format = video_format
                                break
                                
                        if not selected_format and video_formats:
                            selected_format = video_formats[-1]  # Use lowest quality if no 720p
                        
                        if selected_format and "url" in selected_format:
                            video_url = selected_format["url"]
                            logger.info(f"Found video URL via Invidious API: {video_url[:50]}...")
                            
                            # Download video
                            with requests.get(video_url, stream=True) as r:
                                r.raise_for_status()
                                with open(destination, 'wb') as f:
                                    for chunk in r.iter_content(chunk_size=8192):
                                        f.write(chunk)
                            
                            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                                logger.info(f"Invidious API download successful: {os.path.getsize(destination)} bytes")
                                success = True
                                return destination
            
            elif service["download_method"] == "youtube_dl_server":
                # Use a dedicated youtube-dl server if available
                api_url = service["url_format"].format(
                    base_url=service["base_url"],
                    video_id=video_id
                )
                logger.info(f"Requesting from youtube-dl server: {api_url}")
                
                response = requests.get(api_url, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    
                    if "formats" in data:
                        formats = data["formats"]
                        
                        # Filter and sort by quality
                        video_formats = [f for f in formats if f.get("height") and f.get("width")]
                        video_formats.sort(key=lambda x: int(x.get("height", 0)), reverse=True)
                        
                        selected_format = None
                        for video_format in video_formats:
                            if video_format.get("height", 0) <= 720:  # Limit to 720p
                                selected_format = video_format
                                break
                                
                        if not selected_format and video_formats:
                            selected_format = video_formats[-1]  # Use lowest quality if no 720p
                        
                        if selected_format and "url" in selected_format:
                            video_url = selected_format["url"]
                            logger.info(f"Found video URL via youtube-dl server: {video_url[:50]}...")
                            
                            # Download video
                            with requests.get(video_url, stream=True) as r:
                                r.raise_for_status()
                                with open(destination, 'wb') as f:
                                    for chunk in r.iter_content(chunk_size=8192):
                                        f.write(chunk)
                            
                            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                                logger.info(f"youtube-dl server download successful: {os.path.getsize(destination)} bytes")
                                success = True
                                return destination
        
        except Exception as e:
            error_message = f"Error with {service['name']}: {str(e)}"
            logger.warning(error_message)
            error_messages.append(error_message)
    
    # If all proxy services fail, try the regular download method as a last resort
    try:
        logger.info("All proxy services failed. Trying regular download method as last resort.")
        return download_from_youtube(url, destination)
    except Exception as e:
        error_message = f"Regular download method also failed: {str(e)}"
        logger.error(error_message)
        error_messages.append(error_message)
    
    # If we reach here, all methods have failed
    error_details = "\n".join(error_messages)
    logger.error(f"All download methods failed. Errors:\n{error_details}")
    raise Exception(f"Failed to download video from YouTube using all available methods. Try a different video or try again later.")

def process_video(input_path, timestamps, output_path):
    """Process the video based on given timestamps."""
    logger.info(f"Opening video file: {input_path}")
    
    # First, check if the video has audio using ffmpeg directly
    has_audio = False
    try:
        import subprocess
        result = subprocess.run(
            ['ffmpeg', '-i', input_path, '-f', 'null', '-'],
            stderr=subprocess.PIPE,
            text=True
        )
        if "Audio" in result.stderr:
            has_audio = True
            logger.info("Input file has audio track detected by FFmpeg")
        else:
            logger.warning("No audio track detected in input file")
    except Exception as e:
        logger.warning(f"Could not verify audio in input: {str(e)}")
    
    try:
        # Try to extract just the audio first if it exists
        audio_track = None
        if has_audio:
            try:
                temp_audio = tempfile.mktemp(suffix='.m4a')
                os.system(f"ffmpeg -i {input_path} -vn -acodec copy {temp_audio}")
                if os.path.exists(temp_audio) and os.path.getsize(temp_audio) > 0:
                    from moviepy.audio.io.AudioFileClip import AudioFileClip
                    audio_track = AudioFileClip(temp_audio)
                    logger.info(f"Extracted audio track, duration: {audio_track.duration}s")
            except Exception as e:
                logger.warning(f"Could not extract audio track: {str(e)}")
        
        # Open video with explicit audio setting
        video = VideoFileClip(input_path, audio=True)
        logger.info(f"Video loaded: duration={video.duration}s, size={video.size}, fps={video.fps}, has_audio={video.audio is not None}")
        
        # If MoviePy didn't detect audio but we extracted it, use our extracted audio
        if video.audio is None and audio_track is not None:
            video.audio = audio_track
            logger.info("Applied separately extracted audio track to the video")
        
        # Create clips
        clips = []
        for start, end in timestamps:
            logger.info(f"Creating clip from {start}s to {end}s")
            
            # Create subclip
            clip = video.subclip(start, end)
            
            # Ensure audio is present in the clip
            if clip.audio is None and video.audio is not None:
                # If original has audio but clip doesn't, try to manually extract audio segment
                logger.warning(f"Audio missing in clip, trying to manually add audio from {start}s to {end}s")
                try:
                    audio_segment = video.audio.subclip(start, end)
                    clip.audio = audio_segment
                except Exception as e:
                    logger.warning(f"Failed to add audio segment: {str(e)}")
            
            clips.append(clip)
        
        if not clips:
            logger.warning("No clips were created")
            raise Exception("No valid clips to concatenate")
        
        logger.info(f"Concatenating {len(clips)} clips")
        
        # Use the method with explicit audio handling
        final_clip = concatenate_videoclips(clips, method="compose")
        
        # Verify audio in final clip
        if final_clip.audio is None and has_audio:
            logger.warning("Audio was lost during concatenation, trying alternative method")
            # Try another concatenation method
            try:
                final_clip = concatenate_videoclips(clips, method="chain")
            except Exception as e:
                logger.warning(f"Alternative concatenation failed: {str(e)}")
        
        if final_clip:
            logger.info(f"Writing final video to: {output_path}")
            
            # Use high quality settings and ensure audio is included
            final_clip.write_videofile(
                output_path,
                codec='libx264',      # Standard high-quality video codec
                audio_codec='aac',    # Standard high-quality audio codec
                bitrate='8000k',      # High bitrate for good quality
                audio_bitrate='320k', # High audio bitrate
                fps=None,             # Maintain original FPS
                preset='medium',      # Balance between quality and processing time
                threads=2,            # Use multiple threads
                write_logfile=True    # Enable logging for debugging
            )
            
            # Close final clip
            final_clip.close()
        
        # Close the original video
        video.close()
        
        # Clean up temp files
        if audio_track is not None:
            audio_track.close()
            if os.path.exists(temp_audio):
                os.remove(temp_audio)
        
        # Verify the output file has audio
        try:
            result = subprocess.run(
                ['ffmpeg', '-i', output_path, '-f', 'null', '-'],
                stderr=subprocess.PIPE,
                text=True
            )
            if "Audio" in result.stderr:
                logger.info("Output file has audio track verified by FFmpeg")
            else:
                logger.warning("No audio track detected in output file by FFmpeg")
                
                # If no audio detected in output but input had audio, try one more approach
                if has_audio:
                    logger.info("Attempting direct FFmpeg processing as fallback")
                    # Create a list of segment files
                    segments = []
                    for i, (start, end) in enumerate(timestamps):
                        segment = f"/tmp/segment_{i}.mp4"
                        segments.append(segment)
                        duration = end - start
                        cmd = f'ffmpeg -ss {start} -i {input_path} -t {duration} -c copy {segment}'
                        logger.info(f"Executing: {cmd}")
                        os.system(cmd)
                    
                    # Create a file list for concatenation
                    with open('/tmp/segments.txt', 'w') as f:
                        for segment in segments:
                            f.write(f"file '{segment}'\n")
                    
                    # Concatenate using FFmpeg
                    concat_cmd = f'ffmpeg -f concat -safe 0 -i /tmp/segments.txt -c copy {output_path}'
                    logger.info(f"Executing concat: {concat_cmd}")
                    os.system(concat_cmd)
                    
                    # Clean up segment files
                    for segment in segments:
                        if os.path.exists(segment):
                            os.remove(segment)
                    if os.path.exists('/tmp/segments.txt'):
                        os.remove('/tmp/segments.txt')
                    
                    # Verify final output
                    result = subprocess.run(
                        ['ffmpeg', '-i', output_path, '-f', 'null', '-'],
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    if "Audio" in result.stderr:
                        logger.info("FFmpeg fallback: Output file has audio track")
                    else:
                        logger.warning("FFmpeg fallback: Still no audio track detected in output file")
            
        except Exception as e:
            logger.warning(f"Could not verify audio in output: {str(e)}")
        
        return output_path
    
    except Exception as e:
        logger.error(f"Error in video processing: {str(e)}")
        raise Exception(f"Failed to process video: {str(e)}")

def process_video_job(job_id, google_drive_link, timestamps):
    """Background job to process a video."""
    try:
        # Update job status to processing
        jobs[job_id]["status"] = "processing"
        
        # Create a temp directory for downloads
        temp_dir = tempfile.mkdtemp()
        input_file = os.path.join(temp_dir, "input_video.mp4")
        
        try:
            # Download using direct method
            jobs[job_id]["message"] = "Downloading video from Google Drive..."
            logger.info(f"Starting download from: {google_drive_link}")
            
            try:
                file_id = extract_file_id(google_drive_link)
                jobs[job_id]["message"] = f"Extracted file ID: {file_id}, downloading..."
            except Exception as e:
                jobs[job_id]["message"] = f"Warning: Could not extract file ID: {str(e)}"
                
            download_from_google_drive(google_drive_link, input_file)
            
            # Check if file exists and has content
            if not os.path.exists(input_file) or os.path.getsize(input_file) == 0:
                raise Exception("Downloaded file is empty or does not exist")
            
            jobs[job_id]["message"] = f"Download complete. File size: {os.path.getsize(input_file) / (1024*1024):.2f} MB"
            
            # Generate unique output filename
            output_filename = f"{uuid.uuid4()}.mp4"
            output_path = os.path.join(VIDEO_DIR, output_filename)
            
            # Process video
            jobs[job_id]["message"] = f"Processing video with {len(timestamps)} timestamp pairs..."
            logger.info(f"Processing video with {len(timestamps)} timestamp pairs")
            process_video(input_file, timestamps, output_path)
            
            # Use the utility function to generate a download URL that works with both HTTP and HTTPS
            download_url = generate_download_url(output_filename)
            
            # Update job status to complete
            jobs[job_id].update({
                "status": "completed",
                "download_url": download_url,
                "message": "Video processed successfully"
            })
            
        except Exception as e:
            logger.error(f"Error processing video: {str(e)}")
            jobs[job_id].update({
                "status": "failed",
                "message": f"Error processing video: {str(e)}"
            })
        
        finally:
            # Clean up temporary files
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
    
    except Exception as e:
        logger.error(f"Unexpected error in job: {str(e)}")
        jobs[job_id].update({
            "status": "failed",
            "message": f"Unexpected error: {str(e)}"
        })

def process_youtube_job(job_id, youtube_url, timestamps):
    """Background job to process a YouTube video."""
    try:
        # Update job status to processing
        jobs[job_id]["status"] = "processing"
        
        # Create a temp directory for downloads
        temp_dir = tempfile.mkdtemp()
        input_file = os.path.join(temp_dir, "input_video.mp4")
        
        try:
            # Download from YouTube
            jobs[job_id]["message"] = "Downloading video from YouTube..."
            logger.info(f"Starting download from YouTube: {youtube_url}")
            
            download_from_youtube(youtube_url, input_file)
            
            # Check if file exists and has content
            if not os.path.exists(input_file) or os.path.getsize(input_file) == 0:
                raise Exception("Downloaded file is empty or does not exist")
            
            jobs[job_id]["message"] = f"Download complete. File size: {os.path.getsize(input_file) / (1024*1024):.2f} MB"
            
            # Generate unique output filename
            output_filename = f"{uuid.uuid4()}.mp4"
            output_path = os.path.join(VIDEO_DIR, output_filename)
            
            # Process video
            jobs[job_id]["message"] = f"Processing video with {len(timestamps)} timestamp pairs..."
            logger.info(f"Processing video with {len(timestamps)} timestamp pairs")
            process_video(input_file, timestamps, output_path)
            
            # Use the utility function to generate a download URL that works with both HTTP and HTTPS
            download_url = generate_download_url(output_filename)
            
            # Update job status to complete
            jobs[job_id].update({
                "status": "completed",
                "download_url": download_url,
                "message": "YouTube video processed successfully"
            })
            
        except Exception as e:
            logger.error(f"Error processing YouTube video: {str(e)}")
            jobs[job_id].update({
                "status": "failed",
                "message": f"Error processing YouTube video: {str(e)}"
            })
        
        finally:
            # Clean up temporary files
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
    
    except Exception as e:
        logger.error(f"Unexpected error in YouTube job: {str(e)}")
        jobs[job_id].update({
            "status": "failed",
            "message": f"Unexpected error: {str(e)}"
        })

def process_youtube_proxy_job(job_id, youtube_url, timestamps):
    """Background job to process a YouTube video using proxy services."""
    try:
        # Update job status to processing
        jobs[job_id]["status"] = "processing"
        
        # Create a temp directory for downloads
        temp_dir = tempfile.mkdtemp()
        input_file = os.path.join(temp_dir, "input_video.mp4")
        
        try:
            # Download from YouTube using proxy
            jobs[job_id]["message"] = "Downloading video from YouTube via proxy service..."
            logger.info(f"Starting proxy download from YouTube: {youtube_url}")
            
            download_from_youtube_proxy(youtube_url, input_file)
            
            # Check if file exists and has content
            if not os.path.exists(input_file) or os.path.getsize(input_file) == 0:
                raise Exception("Downloaded file is empty or does not exist")
            
            jobs[job_id]["message"] = f"Download complete. File size: {os.path.getsize(input_file) / (1024*1024):.2f} MB"
            
            # Generate unique output filename
            output_filename = f"{uuid.uuid4()}.mp4"
            output_path = os.path.join(VIDEO_DIR, output_filename)
            
            # Process video
            jobs[job_id]["message"] = f"Processing video with {len(timestamps)} timestamp pairs..."
            logger.info(f"Processing video with {len(timestamps)} timestamp pairs")
            process_video(input_file, timestamps, output_path)
            
            # Use the utility function to generate a download URL that works with both HTTP and HTTPS
            download_url = generate_download_url(output_filename)
            
            # Update job status
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["message"] = "Video processing completed successfully"
            jobs[job_id]["output_file"] = output_filename
            jobs[job_id]["download_url"] = download_url
            
            logger.info(f"Job {job_id} completed successfully. Output: {output_path}")
            return output_path
        
        except Exception as e:
            logger.error(f"Error processing YouTube video via proxy: {str(e)}")
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["message"] = f"Error: {str(e)}"
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
        
        finally:
            # Clean up temp directory
            if os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    logger.warning(f"Failed to remove temp directory {temp_dir}: {str(e)}")
    
    except Exception as e:
        logger.error(f"Unhandled exception in process_youtube_proxy_job: {str(e)}")
        if job_id in jobs:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["message"] = f"Unhandled error: {str(e)}"
        raise

@app.route('/process_google_drive', methods=['POST'])
@swag_from({
    'tags': ['Video Processing'],
    'summary': 'Process video from Google Drive',
    'description': 'Downloads a video from Google Drive, cuts segments based on timestamps, and concatenates them. The processing happens asynchronously and returns a job ID that can be used to check status.',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['google_drive_link', 'timestamps'],
                'properties': {
                    'google_drive_link': {
                        'type': 'string',
                        'description': 'Shareable link to a Google Drive video. The link should be in one of these formats: https://drive.google.com/file/d/YOUR_FILE_ID/view or https://drive.google.com/open?id=YOUR_FILE_ID',
                        'example': 'https://drive.google.com/file/d/1VSBCOeRsgplhFlSoWphyk5RkZOJ3FjQZ/view'
                    },
                    'timestamps': {
                        'type': 'array',
                        'description': 'Array of timestamp pairs [start, end] in seconds',
                        'items': {
                            'type': 'array',
                            'items': {'type': 'number'},
                            'minItems': 2,
                            'maxItems': 2
                        },
                        'example': [[10, 20], [30, 45], [60, 70]]
                    }
                }
            }
        }
    ],
    'responses': {
        '202': {
            'description': 'Job accepted for processing',
            'content': {
                'application/json': {
            'schema': {
                'type': 'object',
                'properties': {
                    'job_id': {'type': 'string'},
                    'status': {'type': 'string'},
                    'message': {'type': 'string'},
                    'status_url': {'type': 'string'}
                        }
                    }
                }
            }
        },
        '400': {
            'description': 'Bad request parameters',
            'content': {
                'application/json': {
            'schema': {
                'type': 'object',
                'properties': {
                    'error': {'type': 'string'}
                }
            }
        }
    }
        }
    },
    'schemes': ['https'],  # Force HTTPS in Swagger documentation
    'produces': ['application/json']  # Specify the response content type
})
def process_google_drive():
    try:
        # Log the request details for debugging
        logger.info(f"Received request to /process_google_drive with headers: {dict(request.headers)}")
        
        data = request.get_json()
        
        if not data:
            error_response = jsonify({"error": "No JSON data provided"})
            error_response.headers.add('Access-Control-Allow-Origin', '*')
            return error_response, 400
        
        google_drive_link = data.get('google_drive_link')
        timestamps = data.get('timestamps')
        
        if not google_drive_link:
            error_response = jsonify({"error": "No Google Drive link provided"})
            error_response.headers.add('Access-Control-Allow-Origin', '*')
            return error_response, 400
        
        if not timestamps or not isinstance(timestamps, list):
            error_response = jsonify({"error": "Invalid or missing timestamps"})
            error_response.headers.add('Access-Control-Allow-Origin', '*')
            return error_response, 400
        
        # Create a job ID
        job_id = str(uuid.uuid4())
        
        # Initialize job status
        jobs[job_id] = {
            "status": "queued",
            "message": "Job queued for processing",
            "created_at": time.time()
        }
        
        # Start background thread to process video
        thread = threading.Thread(
            target=process_video_job,
            args=(job_id, google_drive_link, timestamps)
        )
        thread.daemon = True
        thread.start()
        
        # Return job ID and status URL - Force HTTPS for external URLs
        status_url = url_for('job_status', job_id=job_id, _external=True)
        # Ensure URL uses HTTPS regardless of request scheme
        if status_url.startswith('http:'):
            status_url = status_url.replace('http:', 'https:', 1)
            
        response = jsonify({
            "job_id": job_id,
            "status": "queued",
            "message": "Job queued for processing",
            "status_url": status_url
        })
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response, 202
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        error_response = jsonify({"error": f"Unexpected error: {str(e)}"})
        error_response.headers.add('Access-Control-Allow-Origin', '*')
        return error_response, 500

@app.route('/process_youtube', methods=['POST'])
@swag_from({
    'tags': ['Video Processing'],
    'summary': 'Process video from YouTube',
    'description': 'Downloads a video from YouTube at maximum resolution, cuts segments based on timestamps, and concatenates them. The processing happens asynchronously and returns a job ID that can be used to check status.',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['youtube_url', 'timestamps'],
                'properties': {
                    'youtube_url': {
                        'type': 'string',
                        'description': 'YouTube video URL',
                        'example': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
                    },
                    'timestamps': {
                        'type': 'array',
                        'description': 'Array of timestamp pairs [start, end] in seconds',
                        'items': {
                            'type': 'array',
                            'items': {'type': 'number'},
                            'minItems': 2,
                            'maxItems': 2
                        },
                        'example': [[10, 20], [30, 45], [60, 70]]
                    }
                }
            }
        }
    ],
    'responses': {
        '202': {
            'description': 'Job accepted for processing',
            'content': {
                'application/json': {
            'schema': {
                'type': 'object',
                'properties': {
                    'job_id': {'type': 'string'},
                    'status': {'type': 'string'},
                    'message': {'type': 'string'},
                    'status_url': {'type': 'string'}
                        }
                    }
                }
            }
        },
        '400': {
            'description': 'Bad request parameters',
            'content': {
                'application/json': {
            'schema': {
                'type': 'object',
                'properties': {
                    'error': {'type': 'string'}
                }
            }
        }
    }
        }
    },
    'schemes': ['https'],  # Force HTTPS in Swagger documentation
    'produces': ['application/json']  # Specify the response content type
})
def process_youtube():
    try:
        # Log the request details for debugging
        logger.info(f"Received request to /process_youtube with headers: {dict(request.headers)}")
        
        data = request.get_json()
        
        if not data:
            error_response = jsonify({"error": "No JSON data provided"})
            error_response.headers.add('Access-Control-Allow-Origin', '*')
            return error_response, 400
        
        youtube_url = data.get('youtube_url')
        timestamps = data.get('timestamps')
        
        if not youtube_url:
            error_response = jsonify({"error": "No YouTube URL provided"})
            error_response.headers.add('Access-Control-Allow-Origin', '*')
            return error_response, 400
        
        if not timestamps or not isinstance(timestamps, list):
            error_response = jsonify({"error": "Invalid or missing timestamps"})
            error_response.headers.add('Access-Control-Allow-Origin', '*')
            return error_response, 400
        
        # Create a job ID
        job_id = str(uuid.uuid4())
        
        # Initialize job status
        jobs[job_id] = {
            "status": "queued",
            "message": "Job queued for processing",
            "created_at": time.time()
        }
        
        # Start background thread to process video
        thread = threading.Thread(
            target=process_youtube_job,
            args=(job_id, youtube_url, timestamps)
        )
        thread.daemon = True
        thread.start()
        
        # Return job ID and status URL - Force HTTPS for external URLs
        status_url = url_for('job_status', job_id=job_id, _external=True)
        # Ensure URL uses HTTPS regardless of request scheme
        if status_url.startswith('http:'):
            status_url = status_url.replace('http:', 'https:', 1)
            
        response = jsonify({
            "job_id": job_id,
            "status": "queued",
            "message": "Job queued for processing",
            "status_url": status_url
        })
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response, 202
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        error_response = jsonify({"error": f"Unexpected error: {str(e)}"})
        error_response.headers.add('Access-Control-Allow-Origin', '*')
        return error_response, 500

@app.route('/process_youtube_proxy', methods=['POST'])
@swag_from({
    'tags': ['Video Processing'],
    'summary': 'Process video from YouTube using proxy services',
    'description': 'Downloads a video from YouTube using proxy services to bypass restrictions, cuts segments based on timestamps, and concatenates them. The processing happens asynchronously and returns a job ID that can be used to check status.',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['youtube_url', 'timestamps'],
                'properties': {
                    'youtube_url': {
                        'type': 'string',
                        'description': 'YouTube video URL',
                        'example': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
                    },
                    'timestamps': {
                        'type': 'array',
                        'description': 'Array of timestamp pairs [start, end] in seconds',
                        'items': {
                            'type': 'array',
                            'items': {'type': 'number'},
                            'minItems': 2,
                            'maxItems': 2
                        },
                        'example': [[10, 20], [30, 45], [60, 70]]
                    }
                }
            }
        }
    ],
    'responses': {
        '202': {
            'description': 'Job accepted for processing',
            'content': {
                'application/json': {
            'schema': {
                'type': 'object',
                'properties': {
                    'job_id': {'type': 'string'},
                    'status': {'type': 'string'},
                    'message': {'type': 'string'},
                    'status_url': {'type': 'string'}
                        }
                    }
                }
            }
        },
        '400': {
            'description': 'Bad request parameters',
            'content': {
                'application/json': {
            'schema': {
                'type': 'object',
                'properties': {
                    'error': {'type': 'string'}
                }
            }
        }
    }
        }
    },
    'schemes': ['https'],  # Force HTTPS in Swagger documentation
    'produces': ['application/json']  # Specify the response content type
})
def process_youtube_proxy():
    try:
        # Log the request details for debugging
        logger.info(f"Received request to /process_youtube_proxy with headers: {dict(request.headers)}")
        
        data = request.get_json()
        
        if not data:
            error_response = jsonify({"error": "No JSON data provided"})
            error_response.headers.add('Access-Control-Allow-Origin', '*')
            return error_response, 400
        
        youtube_url = data.get('youtube_url')
        timestamps = data.get('timestamps')
        
        if not youtube_url:
            error_response = jsonify({"error": "No YouTube URL provided"})
            error_response.headers.add('Access-Control-Allow-Origin', '*')
            return error_response, 400
        
        if not timestamps or not isinstance(timestamps, list):
            error_response = jsonify({"error": "Invalid or missing timestamps"})
            error_response.headers.add('Access-Control-Allow-Origin', '*')
            return error_response, 400
                
        # Create a job ID
        job_id = str(uuid.uuid4())
        
        # Initialize job status
        jobs[job_id] = {
            "status": "queued",
            "message": "Job queued for processing",
            "created_at": time.time()
        }
        
        # Start background thread to process video
        thread = threading.Thread(
            target=process_youtube_proxy_job,
            args=(job_id, youtube_url, timestamps)
        )
        thread.daemon = True
        thread.start()
        
        # Return job ID and status URL - Force HTTPS for external URLs
        status_url = url_for('job_status', job_id=job_id, _external=True)
        # Ensure URL uses HTTPS regardless of request scheme
        if status_url.startswith('http:'):
            status_url = status_url.replace('http:', 'https:', 1)
            
        response = jsonify({
            "job_id": job_id,
            "status": "queued",
            "message": "Job queued for processing",
            "status_url": status_url
        })
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response, 202
        
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        error_response = jsonify({"error": f"Unexpected error: {str(e)}"})
        error_response.headers.add('Access-Control-Allow-Origin', '*')
        return error_response, 500

def generate_download_url(filename):
    """
    Generate a download URL that works with both HTTP and HTTPS.
    
    Args:
        filename (str): The filename to download
        
    Returns:
        str: The complete download URL with HTTPS
    """
    # Determine the host from request or environment variables
    if request and request.host:
        server_name = request.host
    else:
        server_name = os.environ.get('SERVER_NAME', 'chop.ytboost.top')  # Default to the domain you're using
    
    # Always use HTTPS for security
    protocol = 'https'
    
    # Generate the full URL
    download_url = f"{protocol}://{server_name}/download/{filename}"
    logger.info(f"Generated download URL: {download_url}")
    
    return download_url

@app.route('/job/<job_id>', methods=['GET'])
@swag_from({
    'tags': ['Job Status'],
    'summary': 'Get job status',
    'description': 'Returns the status of a video processing job',
    'parameters': [
        {
            'name': 'job_id',
            'in': 'path',
            'required': True,
            'type': 'string',
            'description': 'ID of the job'
        }
    ],
    'responses': {
        '200': {
            'description': 'Job status',
            'content': {
                'application/json': {
            'schema': {
                'type': 'object',
                'properties': {
                    'job_id': {'type': 'string'},
                    'status': {'type': 'string', 'enum': ['queued', 'processing', 'completed', 'failed']},
                    'message': {'type': 'string'},
                    'download_url': {'type': 'string'}
                        }
                    }
                }
            }
        },
        '404': {
            'description': 'Job not found',
            'content': {
                'application/json': {
            'schema': {
                'type': 'object',
                'properties': {
                    'error': {'type': 'string'}
                }
            }
        }
    }
        }
    },
    'schemes': ['https'],  # Force HTTPS in Swagger documentation
    'produces': ['application/json']  # Specify the response content type
})
def job_status(job_id):
    # Clean up old jobs
    current_time = time.time()
    # Remove jobs older than one hour that are completed or failed
    jobs_to_remove = [j_id for j_id, job_data in jobs.items() 
                     if job_data.get('created_at', 0) < current_time - 3600 and 
                     job_data.get('status') in ['completed', 'failed']]
    
    for j_id in jobs_to_remove:
        try:
            del jobs[j_id]
        except KeyError:
            pass

    if job_id in jobs:
        job_data = jobs[job_id]
        response_data = {
            "job_id": job_id,
            "status": job_data["status"],
            "message": job_data["message"]
        }
        
        # Add download URL if job is completed
        if job_data["status"] == "completed" and "download_url" in job_data:
            download_url = job_data["download_url"]
            # Ensure URL uses HTTPS
            if download_url.startswith('http:'):
                download_url = download_url.replace('http:', 'https:', 1)
            response_data["download_url"] = download_url
        
        response = jsonify(response_data)
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Content-Type', 'application/json')
        return response
    else:
        error_response = jsonify({"error": "Job not found"})
        error_response.headers.add('Access-Control-Allow-Origin', '*')
        error_response.headers.add('Content-Type', 'application/json')
        return error_response, 404

@app.route('/download_url/<job_id>', methods=['GET'])
@swag_from({
    'tags': ['Video Download'],
    'summary': 'Get just the download URL for a processed video',
    'description': 'Returns only the direct download URL for a completed job',
    'parameters': [
        {
            'name': 'job_id',
            'in': 'path',
            'required': True,
            'type': 'string',
            'description': 'ID of the job'
        }
    ],
    'responses': {
        '200': {
            'description': 'Download URL information',
            'content': {
                'application/json': {
            'schema': {
                        'type': 'object',
                        'properties': {
                            'download_url': {'type': 'string'},
                            'success': {'type': 'boolean'}
                        }
                    }
                }
            }
        },
        '202': {
            'description': 'Job is still processing',
            'content': {
                'application/json': {
            'schema': {
                        'type': 'object',
                        'properties': {
                            'status': {'type': 'string'},
                            'message': {'type': 'string'},
                            'success': {'type': 'boolean'}
                        }
                    }
                }
            }
        },
        '404': {
            'description': 'Job not found',
            'content': {
                'application/json': {
            'schema': {
                        'type': 'object',
                        'properties': {
                            'error': {'type': 'string'},
                            'success': {'type': 'boolean'}
                        }
                    }
                }
            }
        },
        '500': {
            'description': 'Job failed',
            'content': {
                'application/json': {
            'schema': {
                        'type': 'object',
                        'properties': {
                            'error': {'type': 'string'},
                            'success': {'type': 'boolean'}
            }
        }
    }
            }
        }
    },
    'schemes': ['https'],  # Force HTTPS in Swagger documentation
    'produces': ['application/json']  # Specify the response content type
})
def download_url(job_id):
    """Endpoint to get just the download URL for a completed job."""
    if job_id not in jobs:
        error_response = jsonify({
            "error": "Job not found",
            "success": False
        })
        # Add CORS headers
        error_response.headers.add('Access-Control-Allow-Origin', '*')
        error_response.headers.add('Content-Type', 'application/json')
        return error_response, 404
    
    job = jobs[job_id]
    
    if job["status"] == "completed" and "download_url" in job:
        # Return the URL in a JSON response
        url_response = jsonify({
            "download_url": job["download_url"],
            "success": True
        })
        url_response.headers.add('Access-Control-Allow-Origin', '*')
        url_response.headers.add('Content-Type', 'application/json')
        return url_response
    elif job["status"] == "failed":
        error_message = job.get('message', 'Unknown error')
        error_response = jsonify({
            "error": f"Job failed: {error_message}",
            "success": False
        })
        error_response.headers.add('Access-Control-Allow-Origin', '*')
        error_response.headers.add('Content-Type', 'application/json')
        return error_response, 500
    else:
        # Job is still processing
        status_response = jsonify({
            "status": job['status'],
            "message": f"Job is {job['status']}, please check back later",
            "success": False
        })
        status_response.headers.add('Access-Control-Allow-Origin', '*')
        status_response.headers.add('Content-Type', 'application/json')
        return status_response, 202

def generate_enhanced_youtube_cookies():
    """
    Generate enhanced YouTube cookies with realistic session IDs.
    This helps bypass YouTube's bot detection by simulating cookies from a real browser session.
    
    Returns:
        str: Path to the generated cookie file
    """
    logger.info("Generating enhanced YouTube cookies")
    
    # Generate random session IDs and values
    import random, string, time
    from datetime import datetime, timedelta
    
    # Calculate expiry timestamp (1 year from now)
    expiry = int((datetime.now() + timedelta(days=365)).timestamp())
    
    # Generate random session IDs
    def random_id(length=10):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))
    
    ysc_id = random_id(10)
    visitor_id = random_id(10)
    psid = random_id(64)
    psidts = random_id(80)
    sid = random_id(24) + "." + random_id(24)
    
    # Create the cookie file
    temp_cookie_file = f"/tmp/enhanced_youtube_cookies_{int(time.time())}.txt"
    with open(temp_cookie_file, 'w') as f:
        f.write(f"""# Netscape HTTP Cookie File
# http://curl.haxx.se/rfc/cookie_spec.html
# This file was generated for YouTube downloads

.youtube.com	TRUE	/	TRUE	0	GPS	1
.youtube.com	TRUE	/	TRUE	0	PREF	f6=40000000&tz=Europe.Stockholm
.youtube.com	TRUE	/	TRUE	0	YSC	{ysc_id}
.youtube.com	TRUE	/	TRUE	{expiry}	CONSENT	PENDING+882
.youtube.com	TRUE	/	TRUE	{expiry}	__Secure-1PSIDTS	{psidts}
.youtube.com	TRUE	/	TRUE	{expiry}	__Secure-3PSIDTS	{psidts}
.youtube.com	TRUE	/	TRUE	{expiry}	VISITOR_INFO1_LIVE	{visitor_id}
.youtube.com	TRUE	/	TRUE	{expiry}	__Secure-1PSID	{psid}
.youtube.com	TRUE	/	TRUE	{expiry}	__Secure-3PSID	{psid}
.youtube.com	TRUE	/	TRUE	{expiry}	APISID	{random_id(20)}
.youtube.com	TRUE	/	TRUE	{expiry}	HSID	{random_id(18)}
.youtube.com	TRUE	/	TRUE	{expiry}	LOGIN_INFO	AFmmF2swRQIhAMLQUTG5KN0Y{random_id(50)}:QUQ3MjNmemJYR3V{random_id(100)}
.youtube.com	TRUE	/	TRUE	{expiry}	SAPISID	{random_id(24)}
.youtube.com	TRUE	/	TRUE	{expiry}	SID	{sid}
.youtube.com	TRUE	/	TRUE	{expiry}	SIDCC	{random_id(60)}
.youtube.com	TRUE	/	TRUE	{expiry}	SSID	{random_id(18)}
.youtube.com	TRUE	/	TRUE	{expiry}	__Secure-1PAPISID	{random_id(24)}
.youtube.com	TRUE	/	TRUE	{expiry}	__Secure-3PAPISID	{random_id(24)}""")
    
    logger.info(f"Generated enhanced cookie file at: {temp_cookie_file}")
    return temp_cookie_file

@app.route('/download/<filename>', methods=['GET', 'OPTIONS'])
@swag_from({
    'tags': ['Video Download'],
    'summary': 'Download a processed video file',
    'description': 'Serves the processed video file for direct download',
    'parameters': [
        {
            'name': 'filename',
            'in': 'path',
            'required': True,
            'type': 'string',
            'description': 'Filename of the processed video'
        }
    ],
    'responses': {
        '200': {
            'description': 'Video file or successful OPTIONS response',
            'content': {
                'video/mp4': {
                    'schema': {
                        'type': 'string',
                        'format': 'binary'
                    }
                },
                'application/json': {
                    'schema': {
                        'type': 'object',
                        'properties': {
                            'status': {'type': 'string'}
                        }
                    }
                }
            }
        },
        '404': {
            'description': 'File not found',
            'content': {
                'application/json': {
                    'schema': {
                        'type': 'object',
                        'properties': {
                            'error': {'type': 'string'},
                            'success': {'type': 'boolean'}
                        }
                    }
                }
            }
        }
    },
    'schemes': ['https'],
    'produces': ['video/mp4', 'application/json']
})
def download_file(filename):
    """Serve a processed video file for download with proper CORS headers."""
    # Handle OPTIONS request for CORS preflight
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'ok'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Methods', 'GET, OPTIONS')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        return response
        
    logging.info(f"Download request for file: {filename}")
    
    try:
        # Check if file exists before attempting to serve it
        file_path = os.path.join(VIDEO_DIR, filename)
        if not os.path.isfile(file_path):
            logging.error(f"File not found: {file_path}")
            response = jsonify({
                'error': 'File not found',
                'success': False
            })
            # Add CORS headers
            response.headers.add('Access-Control-Allow-Origin', '*')
            response.headers.add('Access-Control-Allow-Methods', 'GET')
            response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
            return response, 404
        
        # Serve the file with CORS headers
        response = send_from_directory(
            VIDEO_DIR, 
            filename, 
            as_attachment=True, 
            mimetype='video/mp4'
        )
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Methods', 'GET')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        return response
        
    except Exception as e:
        logging.error(f"Error serving file {filename}: {str(e)}")
        response = jsonify({
            'error': f'Error serving file: {str(e)}',
            'success': False
        })
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Methods', 'GET')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        return response, 500

if __name__ == '__main__':
    # Use gunicorn-compatible settings
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000))) 