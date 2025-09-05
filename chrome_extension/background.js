// Background script for Download Manager Integration
const DOWNLOAD_MANAGER_PORT = 8080;
const DOWNLOAD_MANAGER_HOST = 'localhost';

// Check if download manager is running
async function isDownloadManagerRunning() {
  try {
    const response = await fetch(`http://${DOWNLOAD_MANAGER_HOST}:${DOWNLOAD_MANAGER_PORT}/ping`);
    return response.ok;
  } catch (error) {
    return false;
  }
}

// Send download to download manager
async function sendToDownloadManager(url, filename) {
  try {
    const response = await fetch(`http://${DOWNLOAD_MANAGER_HOST}:${DOWNLOAD_MANAGER_PORT}/add_download`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        url: url,
        filename: filename
      })
    });
    
    if (response.ok) {
      console.log('Download sent to Download Manager successfully');
      return true;
    } else {
      console.error('Failed to send download to Download Manager');
      return false;
    }
  } catch (error) {
    console.error('Error sending download to Download Manager:', error);
    return false;
  }
}

// Intercept downloads
chrome.downloads.onCreated.addListener(async (downloadItem) => {
  // Check if download manager is running
  const isRunning = await isDownloadManagerRunning();
  
  if (isRunning) {
    // Send to download manager
    const success = await sendToDownloadManager(downloadItem.url, downloadItem.filename);
    
    if (success) {
      // Cancel the browser download since we're handling it in the app
      chrome.downloads.cancel(downloadItem.id);
      
      // Show notification
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icon48.png',
        title: 'Download Manager',
        message: `Download added: ${downloadItem.filename}`
      });
    }
  }
});

// Context menu for manual download
chrome.contextMenus.create({
  id: 'sendToDownloadManager',
  title: 'Download with Download Manager',
  contexts: ['link']
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'sendToDownloadManager' && info.linkUrl) {
    const isRunning = await isDownloadManagerRunning();
    
    if (isRunning) {
      const filename = info.linkUrl.split('/').pop() || 'download';
      const success = await sendToDownloadManager(info.linkUrl, filename);
      
      if (success) {
        chrome.notifications.create({
          type: 'basic',
          iconUrl: 'icon48.png',
          title: 'Download Manager',
          message: `Download added: ${filename}`
        });
      }
    } else {
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icon48.png',
        title: 'Download Manager',
        message: 'Download Manager is not running. Please start the app first.'
      });
    }
  }
});

// Handle extension icon click
chrome.action.onClicked.addListener(async (tab) => {
  const isRunning = await isDownloadManagerRunning();
  
  if (isRunning) {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icon48.png',
      title: 'Download Manager',
      message: 'Download Manager is running and ready to receive downloads!'
    });
  } else {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icon48.png',
      title: 'Download Manager',
      message: 'Download Manager is not running. Please start the app first.'
    });
  }
});
