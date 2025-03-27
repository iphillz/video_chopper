# Video Chopper API

A powerful web service for extracting high-quality segments from YouTube videos.

## Features

- Download and process YouTube videos
- Extract video segments with specified timestamps
- Maintain original video quality
- Automatic cleanup after 24 hours
- RESTful API with Swagger documentation

## API Endpoints

- `POST /process_video`: Process a YouTube video segment
- `GET /job/{job_id}`: Check job status
- `GET /download/{filename}`: Download processed video

## Environment Variables

- `DOMAIN`: Your domain name (e.g., api.example.com)
- `SCHEME`: http or https
- `PORT`: Default 3000 for production

## Local Development

1. Create virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # Linux/MacOS
venv\Scripts\activate     # Windows
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Run locally:
```bash
python app.py
```

## Docker Deployment

```bash
docker build -t video-chopper .
docker run -p 3000:3000 video-chopper
```

## Coolify Deployment

1. Connect your repository to Coolify
2. Set environment variables:
   - DOMAIN
   - SCHEME
3. Deploy

## API Documentation

Access Swagger UI at the root URL (/) when the service is running. 