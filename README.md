# Video Clip API

A Python-based API for processing and editing videos from Google Drive. This API allows you to extract specific segments from videos using timestamps and concatenate them into a single output video.

## Features

- Download videos from Google Drive
- Download and process videos from YouTube (with authenticated cookies)
- Process videos by cutting segments based on timestamps
- Concatenate video segments into a single output file
- Provide download links for processed videos
- Containerized with Docker for easy deployment
- Ready for deployment with Coolify
- Interactive API documentation with Swagger UI
- Asynchronous video processing to handle long operations

## 🔴 YouTube Downloads Work With Included Cookies

This repository now includes authenticated YouTube cookies that solve the bot detection issue. The cookies are in the following locations:
- `cookies.txt` (root directory)
- `youtube_cookies.txt` (root directory)
- `auth/cookies.txt` (auth subdirectory)

The downloader will automatically use these cookies when downloading from YouTube, providing a seamless experience without encountering the "Sign in to confirm you're not a bot" error.

**Note**: These cookies will expire in the future (mid-2025) and may need to be refreshed.

## API Documentation

The API includes interactive documentation using Swagger UI, which allows you to explore and test all the endpoints.

- Access the documentation at `/docs` when the server is running
- View detailed information about request parameters and response formats
- Test the API directly from the documentation interface

## API Endpoints

### 1. Process Google Drive Video

**Endpoint:** `POST /process_google_drive`

**Description:** Initiates asynchronous processing of a video from Google Drive by extracting segments based on specified timestamps and concatenating them.

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
  "job_id": "abc123-456def",
  "status": "queued",
  "message": "Job queued for processing",
  "status_url": "/job/abc123-456def"
}
```

### 2. Process YouTube Video

**Endpoint:** `POST /process_youtube`

**Description:** Initiates asynchronous processing of a video from YouTube by extracting segments based on specified timestamps and concatenating them.

**Request Body:**
```json
{
  "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "timestamps": [[10, 20], [30, 45], [60, 70]]
}
```

**Response:**
```json
{
  "job_id": "abc123-456def",
  "status": "queued",
  "message": "Job queued for processing",
  "status_url": "/job/abc123-456def"
}
```

### 3. Check Job Status

**Endpoint:** `GET /job/<job_id>`

**Description:** Checks the status of a video processing job.

**Response:**
```json
{
  "job_id": "abc123-456def",
  "status": "completed",
  "message": "Video processed successfully",
  "download_url": "/download/xyz789.mp4"
}
```

**Possible status values:**
- `queued`: Job is waiting to be processed
- `processing`: Job is currently being processed
- `completed`: Job has completed successfully
- `failed`: Job has failed

### 4. Get Download URL Only

**Endpoint:** `GET /download_url/<job_id>`

**Description:** Returns only the download URL for a processed video as plain text.

**Response (success):**
```
http://your-server.com/download/xyz789.mp4
```

**Other responses:**
- `202`: "Job is processing, please check back later"
- `404`: "Job not found"
- `500`: "Job failed: [error message]"

### 5. Download Processed Video

**Endpoint:** `GET /download/<filename>`

**Description:** Downloads a processed video file.

**Response:** The processed video file.

### 6. Health Check

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

Example link format: `https://drive.google.com/file/d/YOUR_FILE_ID/view`

### Important Notes About Google Drive Links

- The API supports several Google Drive link formats:
  - `https://drive.google.com/file/d/YOUR_FILE_ID/view`
  - `https://drive.google.com/open?id=YOUR_FILE_ID`
  - `https://docs.google.com/file/d/YOUR_FILE_ID/view`

- Ensure the file is shared with "Anyone with the link" permissions
- For large files (>100MB), the processing may take longer
- The API automatically extracts the file ID from your link and creates a direct download URL

### Example

If your Google Drive link is:
```
https://drive.google.com/file/d/1VSBCOeRsgplhFlSoWphyk5RkZOJ3FjQZ/view
```

The API will extract the file ID `1VSBCOeRsgplhFlSoWphyk5RkZOJ3FjQZ` and create a direct download URL automatically.

## Using the API for Video Processing

1. Submit a video processing request to `/process_google_drive` with your Google Drive link and timestamps
2. You'll receive a job ID and a status URL
3. Poll the status URL to check when processing is complete
4. Once the status is "completed", use the provided download URL to get the processed video

This approach allows processing of larger videos without timeout issues.

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
   - Configure the port: Set the published port to `3000`

5. **Deploy the application**
   - Click "Deploy"
   - Wait for the build and deployment to complete

6. **Access your API**
   - Once deployed, Coolify will provide a URL to access your API
   - You can now send requests to the API endpoints
   - Access the Swagger UI documentation at `your-api-url/docs`

### Environment Variables (Optional)

While no environment variables are required by default, you can customize the application by setting the following:

- `PORT`: Change the port the application listens on (default: 3000)
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

4. The API will be available at `http://localhost:3000`
   - Access the Swagger UI documentation at `http://localhost:3000/docs`

## Docker Usage

### Build the Docker image:
```
docker build -t video-clip-api .
```

### Run the container:
```
docker run -p 3000:3000 -d video-clip-api
```

After running the container, you can access:
- The API at `http://localhost:3000`
- Swagger documentation at `http://localhost:3000/docs`

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Troubleshooting YouTube Downloads

### YouTube Bot Detection

When downloading videos from YouTube, you may encounter the "Sign in to confirm you're not a bot" error. This happens because YouTube's anti-bot systems detect automated download attempts.

### Solutions:

1. **Use the Included Cookies**

   This repository already includes authenticated cookie files that should solve most bot detection issues. The downloader will automatically use these files to authenticate with YouTube.

2. **Add Your Own Cookies File**

   If the included cookies expire or don't work for some reason:

   - Install a browser extension like "Get cookies.txt" for Chrome/Firefox
   - Log into YouTube in your browser
   - Use the extension to export cookies as cookies.txt
   - Replace one of these files:
     - `/app/cookies.txt`
     - `/app/youtube_cookies.txt`
     - `/app/auth/cookies.txt`

   Example of creating and adding cookies with Docker:
   ```bash
   # First, export cookies.txt from your browser
   # Then, copy to the container
   docker cp cookies.txt your_container_name:/app/cookies.txt
   ```

3. **Try Google Drive Instead**

   If YouTube consistently blocks your downloads, consider uploading your videos to Google Drive and using the `/process_google_drive` endpoint instead, which is more reliable.

4. **Rate Limiting**

   YouTube may block your downloads if you make too many requests in a short time. Spread out your requests to avoid triggering bot detection. 