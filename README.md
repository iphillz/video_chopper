# Video Clip API

A Python-based API for processing and editing videos from Google Drive. This API allows you to extract specific segments from videos using timestamps and concatenate them into a single output video.

## Features

- Download videos from Google Drive
- Process videos by cutting segments based on timestamps
- Concatenate video segments into a single output file
- Provide download links for processed videos
- Containerized with Docker for easy deployment
- Ready for deployment with Coolify

## API Endpoints

### 1. Process Google Drive Video

**Endpoint:** `POST /process_google_drive`

**Description:** Processes a video from Google Drive by extracting segments based on specified timestamps and concatenating them.

**Request Body:**
```json
{
  "google_drive_link": "https://drive.google.com/file/d/YOUR_FILE_ID/view?usp=sharing",
  "timestamps": [[10, 20], [30, 45], [60, 70]]
}
```

**Response:**
```json
{
  "success": true,
  "download_url": "/download/abc123-456def.mp4",
  "message": "Video processed successfully"
}
```

### 2. Download Processed Video

**Endpoint:** `GET /download/<filename>`

**Description:** Downloads a processed video file.

**Response:** The processed video file.

### 3. Health Check

**Endpoint:** `GET /health`

**Description:** Simple health check endpoint.

**Response:**
```json
{
  "status": "healthy"
}
```

## How to Get a Google Drive Shareable Link

1. Open Google Drive and navigate to the file you want to share.
2. Right-click on the file and select "Share".
3. Click on "Get link" and ensure "Anyone with the link" is selected.
4. Click "Copy link" to copy the shareable link to your clipboard.
5. Use this link in your API request.

Example link format: `https://drive.google.com/file/d/YOUR_FILE_ID/view?usp=sharing`

## Deployment with Coolify

[Coolify](https://coolify.io/) is a self-hostable Heroku/Netlify alternative. Here's how to deploy this API using Coolify:

### Prerequisites

- A server with Coolify installed
- Access to the Coolify dashboard
- A GitHub repository containing this code (you can fork this repository)

### Deployment Steps

1. **Log in to your Coolify dashboard**

2. **Create a new service**
   - Click on "New Resource"
   - Select "Application"
   - Choose "Docker"

3. **Connect your GitHub repository**
   - Select GitHub as the source
   - Choose the repository containing this code
   - Select the branch you want to deploy (usually `main` or `master`)

4. **Configure the deployment**
   - Coolify should automatically detect the Dockerfile
   - Set environment variables if needed (none required by default)
   - Configure the port: Set the published port to `5000`

5. **Deploy the application**
   - Click "Deploy"
   - Wait for the build and deployment to complete

6. **Access your API**
   - Once deployed, Coolify will provide a URL to access your API
   - You can now send requests to the API endpoints

### Environment Variables (Optional)

While no environment variables are required by default, you can customize the application by setting the following:

- `PORT`: Change the port the application listens on (default: 5000)
- `LOG_LEVEL`: Set the logging level (default: INFO)

## Local Development

### Prerequisites

- Python 3.9 or 3.10
- FFmpeg

### Setup

1. Clone the repository:
   ```
   git clone https://github.com/yourusername/video-clip-api.git
   cd video-clip-api
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Run the application:
   ```
   python app.py
   ```

4. The API will be available at `http://localhost:5000`

## Docker Usage

### Build the Docker image:
```
docker build -t video-clip-api .
```

### Run the container:
```
docker run -p 5000:5000 -d video-clip-api
```

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. 