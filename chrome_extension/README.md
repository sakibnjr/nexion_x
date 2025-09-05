# Download Manager Chrome Extension

This Chrome extension integrates with the Linux Download Manager to automatically handle downloads from your browser.

## Installation

1. **Start the Download Manager app first**
   ```bash
   cd /home/sakibnjr/Desktop/nexion_x
   python app.py
   ```

2. **Install the Chrome Extension:**
   - Open Chrome and go to `chrome://extensions/`
   - Enable "Developer mode" (toggle in top-right corner)
   - Click "Load unpacked"
   - Select the `chrome_extension` folder from this directory
   - The extension should now appear in your extensions list

3. **Test the integration:**
   - Click the extension icon in your browser toolbar
   - You should see "✓ Connected to Download Manager"
   - Try clicking on any download link - it should automatically start in your Download Manager app

## Features

- **Automatic Download Interception**: Click any download link and it will be sent to your Download Manager
- **Manual Override**: Hold Ctrl/Cmd while clicking to download normally in the browser
- **Right-click Menu**: Right-click any link and select "Download with Download Manager"
- **Connection Status**: Check if the Download Manager is running via the extension popup

## How It Works

1. The extension intercepts download requests from your browser
2. It sends the download URL and filename to the Download Manager app via HTTP
3. The Download Manager app receives the request and starts the download automatically
4. The browser download is cancelled since it's now handled by the app

## Troubleshooting

- **"Download Manager not running"**: Make sure the Python app is started first
- **Downloads not working**: Check that the HTTP server started successfully (look for "HTTP server started on localhost:8080" in the terminal)
- **Extension not loading**: Make sure you're in Developer mode and selected the correct folder
- **"Could not load icon" error**: The extension now includes proper PNG icons. If you still get this error, try refreshing the extensions page

## Port Configuration

The extension communicates with the Download Manager on port 8080. If you need to change this, modify the `DOWNLOAD_MANAGER_PORT` constant in:
- `background.js`
- `content.js` 
- `popup.js`

And update the port in the Python app's `start_http_server()` method.
