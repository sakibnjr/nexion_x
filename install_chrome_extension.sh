#!/bin/bash

echo "=== Download Manager Chrome Extension Installer ==="
echo ""

# Check if Chrome is installed
if ! command -v google-chrome &> /dev/null && ! command -v chromium-browser &> /dev/null; then
    echo "❌ Chrome or Chromium not found. Please install Chrome first."
    exit 1
fi

echo "✅ Chrome/Chromium found"

# Check if the extension directory exists
if [ ! -d "chrome_extension" ]; then
    echo "❌ Chrome extension directory not found. Please run this script from the nexion_x directory."
    exit 1
fi

echo "✅ Extension files found"

# Create a zip file for easier installation
cd chrome_extension
zip -r ../download_manager_extension.zip . > /dev/null 2>&1
cd ..

echo "✅ Extension packaged as download_manager_extension.zip"

echo ""
echo "=== Installation Instructions ==="
echo ""
echo "1. Start the Download Manager app:"
echo "   python app.py"
echo ""
echo "2. Install the Chrome extension:"
echo "   - Open Chrome and go to chrome://extensions/"
echo "   - Enable 'Developer mode' (toggle in top-right corner)"
echo "   - Click 'Load unpacked'"
echo "   - Select the 'chrome_extension' folder"
echo ""
echo "3. Test the integration:"
echo "   - Click the extension icon in your browser toolbar"
echo "   - You should see '✓ Connected to Download Manager'"
echo "   - Try clicking on any download link"
echo ""
echo "🎉 Setup complete! Your downloads will now automatically start in the Download Manager app."
