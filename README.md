# Video Chopper

A powerful web service for extracting high-quality segments from YouTube videos with precise timestamp control.

## Features

- **High-Quality Video Processing**
  - Maintains original video resolution (up to 4K)
  - Preserves original audio quality
  - Retains original video framerate
  - High-quality H.264 encoding

- **Precise Timestamp Control**
  - Format: HH:MM:SS.mmm
  - Accurate segment extraction
  - Millisecond precision

- **User-Friendly API**
  - Interactive Swagger UI documentation
  - RESTful endpoints
  - Background processing with status updates
  - Downloadable results

## Quick Start

1. Clone the repository:
```bash
git clone https://github.com/iphillz/video_chopper.git
cd video_chopper
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set environment variables (optional):
```bash
export DOMAIN=your-domain.com  # Default: localhost:3000
export SCHEME=https           # Default: http
```

4. Run the application:
```bash
gunicorn app:app --bind 0.0.0.0:3000 --workers 2
```

5. Access the Swagger UI:
```
http://localhost:3000/docs
```

## API Endpoints

### Process Video
```http
POST /process_video
Content-Type: multipart/form-data

Parameters:
- youtube_url: string (required)
- input_timestamp: string (required, format: HH:MM:SS.mmm)
- output_timestamp: string (required, format: HH:MM:SS.mmm)
```

### Check Job Status
```http
GET /job/{job_id}
```

### Download Video
```http
GET /download/{filename}
```

## Example Usage

1. Start a processing job:
```bash
curl -X POST "http://localhost:3000/process_video" \
     -F "youtube_url=https://www.youtube.com/watch?v=example" \
     -F "input_timestamp=00:00:30.000" \
     -F "output_timestamp=00:01:00.000"
```

2. Response:
```json
{
    "job_id": "uuid",
    "status": "queued",
    "message": "Job queued for processing",
    "status_url": "http://localhost:3000/job/uuid"
}
```

## Deployment

### Using Docker

1. Build the image:
```bash
docker build -t video-chopper .
```

2. Run the container:
```bash
docker run -p 3000:3000 \
  -e DOMAIN=your-domain.com \
  -e SCHEME=https \
  video-chopper
```

### Using Coolify

1. Create a new service in Coolify
2. Set environment variables:
   - DOMAIN: your-domain.com
   - SCHEME: https
3. Deploy using the GitHub repository

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| DOMAIN | Your application domain | localhost:3000 |
| SCHEME | URL scheme (http/https) | http |

## Technical Details

- **Video Processing**
  - Uses yt-dlp for YouTube downloads
  - MoviePy for video processing
  - H.264 codec with CRF 17
  - AAC audio codec
  - Multi-threaded processing

- **Storage**
  - Automatic cleanup after 24 hours
  - Temporary file management
  - Job status persistence

## Requirements

See `requirements.txt` for full list:
```
Flask==2.0.3
flask-cors==3.0.10
moviepy==1.0.3
yt-dlp==2023.7.6
gunicorn==21.2.0
flasgger==0.9.5
```

## License

MIT License - see LICENSE file for details

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request 