import os
import re
import uuid
import logging
from flask import Flask, request, jsonify, send_from_directory
import requests
import tempfile
from moviepy.editor import VideoFileClip, concatenate_videoclips
import shutil
from flasgger import Swagger, swag_from

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
    "specs_route": "/docs/"
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
}

swagger = Swagger(app, config=swagger_config, template=swagger_template)

# Directory to store processed videos
VIDEO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos")
os.makedirs(VIDEO_DIR, exist_ok=True)

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

def get_direct_download_url(file_id):
    """Generate a direct download URL from a Google Drive file ID."""
    return f"https://drive.google.com/uc?export=download&id={file_id}"

def download_file(url, destination):
    """Download a file from a URL to a destination path."""
    response = requests.get(url, stream=True)
    response.raise_for_status()
    
    with open(destination, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    
    return destination

def process_video(input_path, timestamps, output_path):
    """Process the video based on given timestamps."""
    video = VideoFileClip(input_path)
    clips = []
    
    for start, end in timestamps:
        clip = video.subclip(start, end)
        clips.append(clip)
    
    final_clip = concatenate_videoclips(clips) if clips else None
    if final_clip:
        final_clip.write_videofile(output_path)
        final_clip.close()
    
    video.close()
    return output_path

@app.route('/process_google_drive', methods=['POST'])
@swag_from({
    'tags': ['Video Processing'],
    'summary': 'Process video from Google Drive',
    'description': 'Downloads a video from Google Drive, cuts segments based on timestamps, and concatenates them',
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
                        'description': 'Shareable link to a Google Drive video',
                        'example': 'https://drive.google.com/file/d/YOUR_FILE_ID/view?usp=sharing'
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
        '200': {
            'description': 'Video processed successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'success': {'type': 'boolean'},
                    'download_url': {'type': 'string'},
                    'message': {'type': 'string'}
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
        },
        '500': {
            'description': 'Server error',
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
        
        # Create a temp directory for downloads
        temp_dir = tempfile.mkdtemp()
        input_file = os.path.join(temp_dir, "input_video.mp4")
        
        try:
            # Extract file ID and generate direct download URL
            file_id = extract_file_id(google_drive_link)
            download_url = get_direct_download_url(file_id)
            
            # Download the video
            logger.info(f"Downloading video from Google Drive: {file_id}")
            download_file(download_url, input_file)
            
            # Generate unique output filename
            output_filename = f"{uuid.uuid4()}.mp4"
            output_path = os.path.join(VIDEO_DIR, output_filename)
            
            # Process video
            logger.info(f"Processing video with {len(timestamps)} timestamp pairs")
            process_video(input_file, timestamps, output_path)
            
            # Generate download URL
            download_url = f"/download/{output_filename}"
            
            return jsonify({
                "success": True,
                "download_url": download_url,
                "message": "Video processed successfully"
            })
            
        except Exception as e:
            logger.error(f"Error processing video: {str(e)}")
            return jsonify({"error": f"Error processing video: {str(e)}"}), 500
        
        finally:
            # Clean up temporary files
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

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
@swag_from({
    'tags': ['System'],
    'summary': 'API root',
    'description': 'Redirects to the API documentation',
    'responses': {
        '302': {
            'description': 'Redirect to documentation'
        }
    }
})
def index():
    """Root endpoint, redirects to API documentation."""
    return jsonify({
        "name": "Video Chopper API",
        "version": "1.0.0",
        "documentation": "/docs/",
        "endpoints": [
            {"path": "/process_google_drive", "method": "POST", "description": "Process video from Google Drive"},
            {"path": "/download/<filename>", "method": "GET", "description": "Download processed video"},
            {"path": "/health", "method": "GET", "description": "Health check"},
            {"path": "/docs/", "method": "GET", "description": "API documentation"}
        ]
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000) 