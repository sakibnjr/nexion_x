// Content script to intercept download links and buttons
(function() {
  'use strict';
  
  const DOWNLOAD_MANAGER_PORT = 8080;
  const DOWNLOAD_MANAGER_HOST = 'localhost';
  
  // Common download link patterns
  const downloadPatterns = [
    /download/i,
    /\.(zip|rar|7z|tar|gz|bz2|xz|deb|rpm|dmg|exe|msi|bin|iso|img)$/i,
    /\.(pdf|doc|docx|xls|xlsx|ppt|pptx)$/i,
    /\.(mp4|avi|mkv|mov|wmv|flv|webm|mp3|wav|flac|aac)$/i,
    /\.(jpg|jpeg|png|gif|bmp|tiff|svg|webp)$/i
  ];
  
  // Check if a URL looks like a download
  function isDownloadLink(url, text) {
    if (!url) return false;
    
    // Check URL patterns
    for (const pattern of downloadPatterns) {
      if (pattern.test(url) || pattern.test(text)) {
        return true;
      }
    }
    
    // Check for common download attributes
    const downloadAttrs = ['download', 'data-download', 'data-file'];
    return downloadAttrs.some(attr => 
      document.querySelector(`[${attr}]`) !== null
    );
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
      
      return response.ok;
    } catch (error) {
      console.error('Error sending download to Download Manager:', error);
      return false;
    }
  }
  
  // Handle click events
  function handleClick(event) {
    const target = event.target;
    const link = target.closest('a');
    
    if (link && link.href) {
      const url = link.href;
      const text = link.textContent || link.title || '';
      const filename = link.getAttribute('download') || 
                     link.getAttribute('data-download') ||
                     url.split('/').pop() || 
                     'download';
      
      if (isDownloadLink(url, text)) {
        // Check if Ctrl/Cmd key is pressed for manual override
        if (event.ctrlKey || event.metaKey) {
          return; // Let browser handle it normally
        }
        
        // Prevent default download
        event.preventDefault();
        event.stopPropagation();
        
        // Send to download manager
        sendToDownloadManager(url, filename).then(success => {
          if (success) {
            // Show visual feedback
            const originalText = link.textContent;
            link.textContent = '✓ Sent to Download Manager';
            link.style.color = '#4CAF50';
            
            setTimeout(() => {
              link.textContent = originalText;
              link.style.color = '';
            }, 2000);
          } else {
            // Fallback to normal download
            window.open(url, '_blank');
          }
        });
      }
    }
  }
  
  // Add click listener
  document.addEventListener('click', handleClick, true);
  
  // Also handle form submissions that might be downloads
  document.addEventListener('submit', function(event) {
    const form = event.target;
    if (form.action && isDownloadLink(form.action, form.textContent)) {
      // This is a download form, let it proceed normally
      // The background script will catch the actual download
    }
  });
  
  console.log('Download Manager content script loaded');
})();
