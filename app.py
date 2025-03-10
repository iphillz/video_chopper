import os
import re
import uuid
import logging
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, url_for
from flask_cors import CORS
import tempfile
from moviepy.editor import VideoFileClip, concatenate_videoclips
import yt_dlp
from flasgger import Swagger, swag_from
import json
import fcntl

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Get domain from environment or use default
DOMAIN = os.environ.get('DOMAIN', 'localhost:3000')
SCHEME = os.environ.get('SCHEME', 'http')

app = Flask(__name__)
CORS(app)

# Configure Flask
app.config['PREFERRED_URL_SCHEME'] = SCHEME

# Directory to store processed videos
VIDEO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos")
os.makedirs(VIDEO_DIR, exist_ok=True)

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
    "schemes": [SCHEME]
}

swagger_template = {
    "swagger": "2.0",
    "info": {
        "title": "Video Chopper API",
        "description": "API for processing YouTube videos with timestamp-based chopping",
        "version": "2.0.0",
        "contact": {
            "name": "API Support",
            "url": f"{SCHEME}://{DOMAIN}/docs"
        }
    },
    "host": DOMAIN,
    "basePath": "/",
    "schemes": [SCHEME],
    "tags": [
        {
            "name": "Video Processing",
            "description": "Endpoints for processing YouTube videos"
        },
        {
            "name": "Job Management",
            "description": "Endpoints for checking job status"
        }
    ],
    "definitions": {
        "ProcessRequest": {
            "type": "object",
            "properties": {
                "youtube_url": {
                    "type": "string",
                    "description": "YouTube video URL"
                },
                "input_timestamp": {
                    "type": "string",
                    "description": "Input timestamp in format HH:MM:SS.mmm",
                    "pattern": "^\\d{2}:\\d{2}:\\d{2}\\.\\d{3}$"
                },
                "output_timestamp": {
                    "type": "string",
                    "description": "Output timestamp in format HH:MM:SS.mmm",
                    "pattern": "^\\d{2}:\\d{2}:\\d{2}\\.\\d{3}$"
                }
            },
            "required": ["youtube_url", "input_timestamp", "output_timestamp"]
        },
        "JobResponse": {
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

# Jobs storage
JOBS_FILE = os.path.join(os.path.dirname(__file__), 'jobs.json')

def timestamp_to_seconds(timestamp):
    """Convert timestamp string (HH:MM:SS.mmm) to seconds."""
    h, m, s = timestamp.split(':')
    seconds = float(s) + int(m) * 60 + int(h) * 3600
    return seconds

def save_jobs():
    """Save jobs to persistent storage with proper locking."""
    try:
        temp_file = f"{JOBS_FILE}.tmp"
        with open(temp_file, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(jobs, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        
        os.chmod(temp_file, 0o666)
        os.replace(temp_file, JOBS_FILE)
        os.chmod(JOBS_FILE, 0o666)
        return True
    except Exception as e:
        logger.error(f"Error saving jobs: {str(e)}")
        return False

def load_jobs():
    """Load jobs from persistent storage."""
    global jobs
    try:
        if not os.path.exists(JOBS_FILE):
            jobs = {}
            save_jobs()
            return jobs
            
        with open(JOBS_FILE, 'r') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                jobs = json.loads(f.read().strip() or '{}')
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return jobs
    except Exception as e:
        logger.error(f"Error loading jobs: {str(e)}")
        jobs = {}
        return jobs

def cleanup_old_videos():
    """Clean up videos older than 24 hours."""
    try:
        current_time = datetime.now()
        for job_id, job in jobs.items():
            if job.get('created_at'):
                created_time = datetime.fromisoformat(job['created_at'])
                if (current_time - created_time) > timedelta(hours=24):
                    # Delete the video file
                    output_file = job.get('output_file')
                    if output_file and os.path.exists(os.path.join(VIDEO_DIR, output_file)):
                        os.remove(os.path.join(VIDEO_DIR, output_file))
                    # Update job status
                    job['status'] = 'expired'
                    job['message'] = 'Video deleted after 24 hours'
                    save_jobs()
    except Exception as e:
        logger.error(f"Error cleaning up old videos: {str(e)}")

def update_job_status(job_id, status, message=None, download_url=None, output_file=None):
    """Update job status with proper error handling."""
    try:
        if job_id not in jobs:
            jobs[job_id] = {
                'status': status,
                'message': message or '',
                'created_at': datetime.now().isoformat()
            }
        else:
            jobs[job_id]['status'] = status
            if message is not None:
                jobs[job_id]['message'] = message
                
        if download_url is not None:
            jobs[job_id]['download_url'] = download_url
        if output_file is not None:
            jobs[job_id]['output_file'] = output_file
            
        save_jobs()
        return True
    except Exception as e:
        logger.error(f"Error updating job status: {str(e)}")
        return False

def get_download_url(filename):
    """Generate download URL without requiring request context."""
    return f"{SCHEME}://{DOMAIN}/download/{filename}"

def get_status_url(job_id):
    """Generate status URL without requiring request context."""
    return f"{SCHEME}://{DOMAIN}/job/{job_id}"

@app.route('/process_video', methods=['POST'])
@swag_from({
    'tags': ['Video Processing'],
    'summary': 'Process YouTube video',
    'description': 'Download and process a YouTube video using input and output timestamps',
    'parameters': [
        {
            'name': 'youtube_url',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'YouTube video URL'
        },
        {
            'name': 'input_timestamp',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Input timestamp (HH:MM:SS.mmm)'
        },
        {
            'name': 'output_timestamp',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Output timestamp (HH:MM:SS.mmm)'
        }
    ],
    'responses': {
        '200': {
            'description': 'Job created successfully',
            'schema': {'$ref': '#/definitions/JobResponse'}
        },
        '400': {
            'description': 'Invalid request parameters'
        }
    }
})
def process_video():
    try:
        # Clean up old videos first
        cleanup_old_videos()
        
        # Get form data
        youtube_url = request.form.get('youtube_url')
        input_timestamp = request.form.get('input_timestamp')
        output_timestamp = request.form.get('output_timestamp')
        
        if not all([youtube_url, input_timestamp, output_timestamp]):
            return jsonify({
                'error': 'Missing required parameters'
            }), 400
            
        # Validate timestamps format
        timestamp_pattern = re.compile(r'^\d{2}:\d{2}:\d{2}\.\d{3}$')
        if not all(timestamp_pattern.match(ts) for ts in [input_timestamp, output_timestamp]):
            return jsonify({
                'error': 'Invalid timestamp format. Use HH:MM:SS.mmm'
            }), 400
            
        # Convert timestamps to seconds
        start_time = timestamp_to_seconds(input_timestamp)
        end_time = timestamp_to_seconds(output_timestamp)
        
        if end_time <= start_time:
            return jsonify({
                'error': 'Output timestamp must be greater than input timestamp'
            }), 400

        # Generate job ID
        job_id = str(uuid.uuid4())
        update_job_status(job_id, 'queued', 'Job queued for processing')
        
        def process():
            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    # Download video with best quality
                    input_path = os.path.join(temp_dir, f"input_{job_id}.mp4")
                    update_job_status(job_id, 'processing', 'Downloading video from YouTube')
                    
                    ydl_opts = {
                        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                        'outtmpl': input_path,
                        'merge_output_format': 'mp4',
                        'quiet': True
                    }
                    
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([youtube_url])
                    
                    # Process video
                    update_job_status(job_id, 'processing', 'Processing video segment')
                    output_path = os.path.join(VIDEO_DIR, f"{job_id}.mp4")
                    
                    # Load video with audio
                    video = VideoFileClip(input_path)
                    
                    # Get original video properties
                    original_fps = video.fps
                    original_audio = video.audio
                    
                    # Create subclip with audio
                    clip = video.subclip(start_time, end_time)
                    
                    # Ensure audio is copied
                    if original_audio is not None:
                        clip = clip.set_audio(clip.audio)
                    
                    # Write video with original quality settings
                    clip.write_videofile(
                        output_path,
                        fps=original_fps,  # Maintain original FPS
                        codec='libx264',   # Use H.264 codec
                        audio_codec='aac', # Use AAC for audio
                        preset='slow',     # Better quality compression
                        bitrate=None,      # Maintain source bitrate
                        audio=True,        # Ensure audio is included
                        threads=2,         # Use multiple threads
                        ffmpeg_params=[    # Maintain quality
                            '-crf', '17',  # High quality (0-51, lower is better)
                            '-pix_fmt', 'yuv420p'  # Standard pixel format
                        ]
                    )
                    
                    # Clean up
                    video.close()
                    clip.close()
                    
                    # Update job status with download URL
                    output_filename = f"{job_id}.mp4"
                    download_url = get_download_url(output_filename)
                    update_job_status(job_id, 'completed', 'Video processed successfully', 
                                   download_url=download_url, output_file=output_filename)
                    
            except Exception as e:
                logger.error(f"Error processing job {job_id}: {str(e)}")
                update_job_status(job_id, 'failed', f"Processing failed: {str(e)}")
        
        thread = threading.Thread(target=process)
        thread.start()
        
        # Return initial response with status URL
        status_url = get_status_url(job_id)
        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Job queued for processing',
            'status_url': status_url
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
            'schema': {'$ref': '#/definitions/JobResponse'}
        },
        '404': {
            'description': 'Job not found'
        }
    }
})
def get_job_status(job_id):
    cleanup_old_videos()  # Clean up old videos when checking status
    job = jobs.get(job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)

@app.route('/download/<filename>')
def download_file(filename):
    cleanup_old_videos()  # Clean up old videos before download
    try:
        return send_from_directory(VIDEO_DIR, filename, as_attachment=True)
    except Exception as e:
        return str(e), 404

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy"})

# Load jobs at startup
jobs = load_jobs() 