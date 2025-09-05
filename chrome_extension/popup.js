// Popup script for Download Manager Integration
const DOWNLOAD_MANAGER_PORT = 8080;
const DOWNLOAD_MANAGER_HOST = 'localhost';

async function checkConnection() {
  const statusDiv = document.getElementById('status');
  const checkButton = document.getElementById('checkConnection');
  
  statusDiv.textContent = 'Checking connection...';
  statusDiv.className = 'status';
  
  try {
    const response = await fetch(`http://${DOWNLOAD_MANAGER_HOST}:${DOWNLOAD_MANAGER_PORT}/ping`);
    
    if (response.ok) {
      statusDiv.textContent = '✓ Connected to Download Manager';
      statusDiv.className = 'status connected';
    } else {
      throw new Error('Server responded with error');
    }
  } catch (error) {
    statusDiv.textContent = '✗ Download Manager not running';
    statusDiv.className = 'status disconnected';
  }
}

// Check connection when popup opens
document.addEventListener('DOMContentLoaded', checkConnection);

// Add click handler for check button
document.getElementById('checkConnection').addEventListener('click', checkConnection);
