import os
import re
import uuid
import logging
import threading
import time
from flask import Flask, request, jsonify, send_from_directory, redirect, url_for
import requests
import tempfile
from moviepy.editor import VideoFileClip, concatenate_videoclips
import shutil
from flasgger import Swagger, swag_from
import yt_dlp

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

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
    "schemes": ["http", "https"],
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
            
            # Generate download URL - use full URL including hostname
            # Get the server name from request context or use environment variable
            server_name = os.environ.get('SERVER_NAME', 'video_chopper_cooify.saastify.co')
            protocol = os.environ.get('PROTOCOL', 'http')
            download_url = f"{protocol}://{server_name}/download/{output_filename}"
            
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
            'schema': {
                'type': 'object',
                'properties': {
                    'job_id': {'type': 'string'},
                    'status': {'type': 'string'},
                    'message': {'type': 'string'},
                    'status_url': {'type': 'string'}
                }
            }
        },
        '400': {
            'description': 'Bad request parameters',
            'schema': {
                'type': 'object',
                'properties': {
                    'error': {'type': 'string'}
                }
            }
        }
    }
})
def process_google_drive():
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
        
        google_drive_link = data.get('google_drive_link')
        timestamps = data.get('timestamps')
        
        if not google_drive_link:
            return jsonify({"error": "No Google Drive link provided"}), 400
        
        if not timestamps or not isinstance(timestamps, list):
            return jsonify({"error": "Invalid or missing timestamps"}), 400
        
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
        
        # Return job ID and status URL
        status_url = url_for('job_status', job_id=job_id, _external=True)
        return jsonify({
            "job_id": job_id,
            "status": "queued",
            "message": "Job queued for processing",
            "status_url": status_url
        }), 202
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

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
            'schema': {
                'type': 'object',
                'properties': {
                    'job_id': {'type': 'string'},
                    'status': {'type': 'string', 'enum': ['queued', 'processing', 'completed', 'failed']},
                    'message': {'type': 'string'},
                    'download_url': {'type': 'string'}
                }
            }
        },
        '404': {
            'description': 'Job not found',
            'schema': {
                'type': 'object',
                'properties': {
                    'error': {'type': 'string'}
                }
            }
        }
    }
})
def job_status(job_id):
    """Endpoint to check the status of a job."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    
    job = jobs[job_id].copy()
    job["job_id"] = job_id
    
    # Clean up old jobs that are complete or failed and older than 1 hour
    current_time = time.time()
    for jid in list(jobs.keys()):
        j = jobs[jid]
        if j["status"] in ["completed", "failed"] and current_time - j.get("created_at", 0) > 3600:
            jobs.pop(jid, None)
    
    return jsonify(job)

@app.route('/download/<filename>', methods=['GET'])
@swag_from({
    'tags': ['Video Download'],
    'summary': 'Download processed video',
    'description': 'Downloads a processed video file by filename',
    'parameters': [
        {
            'name': 'filename',
            'in': 'path',
            'required': True,
            'type': 'string',
            'description': 'Name of the processed video file'
        }
    ],
    'responses': {
        '200': {
            'description': 'Video file'
        },
        '404': {
            'description': 'File not found',
            'schema': {
                'type': 'object',
                'properties': {
                    'error': {'type': 'string'}
                }
            }
        }
    }
})
def download(filename):
    """Endpoint to download a processed video file."""
    try:
        return send_from_directory(VIDEO_DIR, filename, as_attachment=True)
    except Exception as e:
        logger.error(f"Error serving file: {str(e)}")
        return jsonify({"error": f"Error serving file: {str(e)}"}), 404

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
            'description': 'Download URL',
            'schema': {
                'type': 'string'
            }
        },
        '202': {
            'description': 'Job is still processing',
            'schema': {
                'type': 'string'
            }
        },
        '404': {
            'description': 'Job not found',
            'schema': {
                'type': 'string'
            }
        },
        '500': {
            'description': 'Job failed',
            'schema': {
                'type': 'string'
            }
        }
    }
})
def download_url(job_id):
    """Endpoint to get just the download URL for a completed job."""
    if job_id not in jobs:
        return "Job not found", 404
    
    job = jobs[job_id]
    
    if job["status"] == "completed" and "download_url" in job:
        # Return just the URL as plain text for easy integration
        return job["download_url"]
    elif job["status"] == "failed":
        return f"Job failed: {job.get('message', 'Unknown error')}", 500
    else:
        # Job is still processing
        return f"Job is {job['status']}, please check back later", 202

@app.route('/health', methods=['GET'])
@swag_from({
    'tags': ['System'],
    'summary': 'Health check',
    'description': 'Returns the status of the API',
    'responses': {
        '200': {
            'description': 'API status',
            'schema': {
                'type': 'object',
                'properties': {
                    'status': {'type': 'string'}
                }
            }
        }
    }
})
def health_check():
    """Health check endpoint."""
    return jsonify({"status": "healthy"})

@app.route('/', methods=['GET'])
def index():
    """Root endpoint, redirects to API documentation."""
    return jsonify({
        "name": "Video Chopper API",
        "version": "1.0.0",
        "documentation": "/docs",
        "endpoints": [
            {"path": "/process_google_drive", "method": "POST", "description": "Process video from Google Drive"},
            {"path": "/job/<job_id>", "method": "GET", "description": "Check job status"},
            {"path": "/download/<filename>", "method": "GET", "description": "Download processed video"},
            {"path": "/download_url/<job_id>", "method": "GET", "description": "Get just the download URL for a processed video"},
            {"path": "/health", "method": "GET", "description": "Health check"},
            {"path": "/docs", "method": "GET", "description": "API documentation"}
        ]
    })

# Add a simple route that redirects to /docs
@app.route('/swagger', methods=['GET'])
def swagger_ui():
    return redirect('/docs', code=302)

if __name__ == '__main__':
    # Use gunicorn-compatible settings
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000))) 