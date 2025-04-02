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
from pathlib import Path

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

def process_video_task(job_id, youtube_url, start_time, end_time):
    jobs = load_jobs()
    output_path = os.path.join(VIDEO_DIR, f"{job_id}.mp4")
    
    try:
        # Configure yt-dlp with better options
        ydl_opts = {
            'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best',  # More reliable format selection
            'merge_output_format': 'mp4',
            'outtmpl': os.path.join(VIDEO_DIR, f"temp_{job_id}.%(ext)s"),
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'no_color': True,
            # Add cookies for better access
            'cookiesfrombrowser': ('chrome',),  # This will use Chrome cookies if available
            # Add more options to handle signature extraction issues
            'extractor_retries': 5,
            'fragment_retries': 5,
            'skip_unavailable_fragments': True,
        }
        
        logger.info(f"Starting download for job {job_id}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # First get video info to check availability
            try:
                video_info = ydl.extract_info(youtube_url, download=False)
                logger.info(f"Video info extracted successfully for {youtube_url}")
            except Exception as e:
                logger.error(f"Failed to extract video info: {str(e)}")
                raise

            # Download the video
            ydl.download([youtube_url])
            
        input_path = os.path.join(VIDEO_DIR, f"temp_{job_id}.mp4")
        if not os.path.exists(input_path):
            raise Exception("Downloaded video file not found")

        logger.info(f"Video downloaded successfully, processing clip for job {job_id}")
        
        # Process video with moviepy
        with VideoFileClip(input_path) as video:
            # Convert timestamps to seconds
            start_seconds = sum(x * float(t) for x, t in zip([3600, 60, 1], start_time.split(":")))
            end_seconds = sum(x * float(t) for x, t in zip([3600, 60, 1], end_time.split(":")))
            
            # Validate timestamps against video duration
            if end_seconds > video.duration:
                end_seconds = video.duration
            if start_seconds >= video.duration:
                raise ValueError("Start timestamp is beyond video duration")

            # Cut video
            new_video = video.subclip(start_seconds, end_seconds)
            new_video.write_videofile(
                output_path,
                codec='libx264',
                audio_codec='aac',
                temp_audiofile=os.path.join(VIDEO_DIR, f'temp_{job_id}.m4a'),
                remove_temp=True,
                logger=None  # Disable moviepy logging
            )
        
        # Clean up temporary files
        if os.path.exists(input_path):
            os.remove(input_path)
        
        # Update job status
        jobs[job_id].update({
            'status': 'completed',
            'download_url': get_download_url(f"{job_id}.mp4"),
            'message': 'Video processed successfully'
        })
        
    except Exception as e:
        logger.error(f"Error processing job {job_id}: {str(e)}")
        jobs[job_id].update({
            'status': 'failed',
            'error': str(e),
            'message': 'Failed to process video'
        })
    
    finally:
        save_jobs()
        logger.info(f"Job {job_id} processing completed with status: {jobs[job_id]['status']}")

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
            'description': 'Input timestamp (HH:MM:SS)'
        },
        {
            'name': 'output_timestamp',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Output timestamp (HH:MM:SS)'
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
    """
    Process YouTube video
    ---
    tags:
      - Video Processing
    consumes:
      - application/x-www-form-urlencoded
      - multipart/form-data
    parameters:
      - name: youtube_url
        in: formData
        type: string
        required: true
        description: YouTube video URL
      - name: input_timestamp
        in: formData
        type: string
        required: true
        description: Start timestamp (HH:MM:SS)
      - name: output_timestamp
        in: formData
        type: string
        required: true
        description: End timestamp (HH:MM:SS)
    responses:
      200:
        description: Job created successfully
        schema:
          type: object
          properties:
            job_id:
              type: string
            status:
              type: string
            message:
              type: string
            check_status_url:
              type: string
      400:
        description: Bad request
    """
    app.logger.info('Received request data: %s', request.form)
    try:
        youtube_url = request.form['youtube_url']
        input_timestamp = request.form['input_timestamp']
        output_timestamp = request.form['output_timestamp']
        
        # Generate a unique job ID
        job_id = str(uuid.uuid4())
        
        # Initialize job
        update_job_status(job_id, 'queued', 'Job created successfully')
        
        # Start processing in a separate thread
        threading.Thread(
            target=process_video_task,
            args=(job_id, youtube_url, input_timestamp, output_timestamp),
            daemon=True
        ).start()
        
        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Job created successfully',
            'check_status_url': get_status_url(job_id)
        }), 200
        
    except Exception as e:
        app.logger.error('Error processing request: %s', str(e))
        return jsonify({
            'error': str(e),
            'message': 'Invalid request parameters'
        }), 400

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

# New function for downloading 1080p videos
def download_1080p_task(job_id, youtube_url):
    jobs = load_jobs()
    output_path = os.path.join(VIDEO_DIR, f"{job_id}_1080p.mp4")
    
    try:
        # Configure yt-dlp with options for 1080p
        ydl_opts = {
            'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
            'merge_output_format': 'mp4',
            'outtmpl': output_path,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'extractor_retries': 5,
            'fragment_retries': 5,
            'skip_unavailable_fragments': True,
        }
        
        logger.info(f"Starting 1080p download for job {job_id}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # First get video info to check availability
            try:
                video_info = ydl.extract_info(youtube_url, download=False)
                logger.info(f"Video info extracted successfully for {youtube_url}")
            except Exception as e:
                logger.error(f"Failed to extract video info: {str(e)}")
                raise
            
            # Download the video
            ydl.download([youtube_url])
        
        # Update job status
        jobs[job_id].update({
            'status': 'completed',
            'download_url': get_download_url(f"{job_id}_1080p.mp4"),
            'message': '1080p video downloaded successfully'
        })
        
    except Exception as e:
        logger.error(f"Error processing 1080p download job {job_id}: {str(e)}")
        jobs[job_id].update({
            'status': 'failed',
            'error': str(e),
            'message': 'Failed to download 1080p video'
        })
    
    finally:
        save_jobs()
        logger.info(f"1080p download job {job_id} completed with status: {jobs[job_id]['status']}")

# New function for downloading MP3 audio
def download_mp3_task(job_id, youtube_url):
    jobs = load_jobs()
    output_path = os.path.join(VIDEO_DIR, f"{job_id}.mp3")
    
    try:
        # Configure yt-dlp with options for MP3
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(VIDEO_DIR, f"{job_id}"),
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'extractor_retries': 5,
            'fragment_retries': 5,
            'skip_unavailable_fragments': True,
        }
        
        logger.info(f"Starting MP3 download for job {job_id}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # First get video info to check availability
            try:
                video_info = ydl.extract_info(youtube_url, download=False)
                logger.info(f"Video info extracted successfully for {youtube_url}")
            except Exception as e:
                logger.error(f"Failed to extract video info: {str(e)}")
                raise
            
            # Download the audio
            ydl.download([youtube_url])
        
        # Update job status
        jobs[job_id].update({
            'status': 'completed',
            'download_url': get_download_url(f"{job_id}.mp3"),
            'message': 'MP3 audio downloaded successfully'
        })
        
    except Exception as e:
        logger.error(f"Error processing MP3 download job {job_id}: {str(e)}")
        jobs[job_id].update({
            'status': 'failed',
            'error': str(e),
            'message': 'Failed to download MP3 audio'
        })
    
    finally:
        save_jobs()
        logger.info(f"MP3 download job {job_id} completed with status: {jobs[job_id]['status']}")

@app.route('/download_1080p', methods=['POST'])
@swag_from({
    'tags': ['Video Processing'],
    'summary': 'Download YouTube video in 1080p',
    'description': 'Download a YouTube video in 1080p resolution',
    'parameters': [
        {
            'name': 'youtube_url',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'YouTube video URL'
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
def download_1080p():
    """
    Download YouTube video in 1080p
    ---
    tags:
      - Video Processing
    consumes:
      - application/x-www-form-urlencoded
      - multipart/form-data
    parameters:
      - name: youtube_url
        in: formData
        type: string
        required: true
        description: YouTube video URL
    responses:
      200:
        description: Job created successfully
        schema:
          type: object
          properties:
            job_id:
              type: string
            status:
              type: string
            message:
              type: string
            check_status_url:
              type: string
      400:
        description: Bad request
    """
    app.logger.info('Received 1080p download request: %s', request.form)
    try:
        youtube_url = request.form['youtube_url']
        
        # Generate a unique job ID
        job_id = str(uuid.uuid4())
        
        # Initialize job
        update_job_status(job_id, 'queued', 'Job created successfully')
        
        # Start processing in a separate thread
        threading.Thread(
            target=download_1080p_task,
            args=(job_id, youtube_url),
            daemon=True
        ).start()
        
        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Job created successfully',
            'check_status_url': get_status_url(job_id)
        }), 200
        
    except Exception as e:
        app.logger.error('Error processing 1080p download request: %s', str(e))
        return jsonify({
            'error': str(e),
            'message': 'Invalid request parameters'
        }), 400

@app.route('/download_mp3', methods=['POST'])
@swag_from({
    'tags': ['Video Processing'],
    'summary': 'Download YouTube audio as MP3',
    'description': 'Extract and download audio from a YouTube video as MP3',
    'parameters': [
        {
            'name': 'youtube_url',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'YouTube video URL'
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
def download_mp3():
    """
    Download YouTube audio as MP3
    ---
    tags:
      - Video Processing
    consumes:
      - application/x-www-form-urlencoded
      - multipart/form-data
    parameters:
      - name: youtube_url
        in: formData
        type: string
        required: true
        description: YouTube video URL
    responses:
      200:
        description: Job created successfully
        schema:
          type: object
          properties:
            job_id:
              type: string
            status:
              type: string
            message:
              type: string
            check_status_url:
              type: string
      400:
        description: Bad request
    """
    app.logger.info('Received MP3 download request: %s', request.form)
    try:
        youtube_url = request.form['youtube_url']
        
        # Generate a unique job ID
        job_id = str(uuid.uuid4())
        
        # Initialize job
        update_job_status(job_id, 'queued', 'Job created successfully')
        
        # Start processing in a separate thread
        threading.Thread(
            target=download_mp3_task,
            args=(job_id, youtube_url),
            daemon=True
        ).start()
        
        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Job created successfully',
            'check_status_url': get_status_url(job_id)
        }), 200
        
    except Exception as e:
        app.logger.error('Error processing MP3 download request: %s', str(e))
        return jsonify({
            'error': str(e),
            'message': 'Invalid request parameters'
        }), 400

# Load jobs at startup
jobs = load_jobs()