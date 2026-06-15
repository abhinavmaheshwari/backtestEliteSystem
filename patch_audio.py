import os
import re

def patch_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    audio_script = """
let _audioCtx = null;
function initAudio() {
    if (!_audioCtx) {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (AudioContext) _audioCtx = new AudioContext();
    }
    if (_audioCtx && _audioCtx.state === 'suspended') _audioCtx.resume();
}
document.addEventListener('click', initAudio, { once: true });

function playDing() {
    if (!_audioCtx) initAudio();
    if (!_audioCtx || _audioCtx.state === 'suspended') return;
    const osc = _audioCtx.createOscillator();
    const gain = _audioCtx.createGain();
    osc.connect(gain);
    gain.connect(_audioCtx.destination);
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, _audioCtx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(110, _audioCtx.currentTime + 0.5);
    gain.gain.setValueAtTime(0.3, _audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, _audioCtx.currentTime + 0.5);
    osc.start();
    osc.stop(_audioCtx.currentTime + 0.5);
}
"""

    if "function playDing" not in content:
        content = content.replace("</script>", audio_script + "\n</script>", 1)

    if "user_dashboard.html" in filepath or "wealth_dashboard.html" in filepath:
        content = content.replace("isAlertActive = true;", "isAlertActive = true;\n    if (typeof playDing === 'function') playDing();")
        
    if "admin_dashboard.html" in filepath:
        if "function triggerAlertNotification" not in content:
            admin_script = """
let isAlertActive = false;
let originalTitle = document.title;
let alertInterval;
function triggerAlertNotification() {
    if(isAlertActive) return;
    isAlertActive = true;
    if (typeof playDing === 'function') playDing();
    let toggle = false;
    alertInterval = setInterval(() => {
        document.title = toggle ? '⚠️ NEW ALERT!' : 'System Admin';
        toggle = !toggle;
    }, 1000);
}
function dismissAlert() {
    if(!isAlertActive) return;
    isAlertActive = false;
    clearInterval(alertInterval);
    document.title = originalTitle;
}
document.addEventListener('click', dismissAlert);
"""
            content = content.replace("async function loadScannerStatus", admin_script + "\nasync function loadScannerStatus")
        
        # Patch setInterval
        interval_search = "setInterval(async () => {\n    const live = await loadScannerStatus();"
        if interval_search in content and "prevTotalAlerts" not in content:
            interval_replace = """
  let prevTotalAlerts = -1;
  setInterval(async () => {
    const live = await loadScannerStatus();
    if (live) {
      window._scannerStats = {...(window._scannerStats||{}), ...live};
      let currentAlerts = 0;
      for (const [key, val] of Object.entries(window._scannerStats)) {
          if (key !== 'AI Worker' && !key.startsWith('API_')) {
              currentAlerts += val.today_alerts || 0;
          }
      }
      if (prevTotalAlerts !== -1 && currentAlerts > prevTotalAlerts) {
          if (typeof triggerAlertNotification === 'function') triggerAlertNotification();
      }
      prevTotalAlerts = currentAlerts;
      renderScannerGrid(window._scannerStats);
"""
            content = content.replace(interval_search + "\n    if (live) {\n      window._scannerStats = {...(window._scannerStats||{}), ...live};\n      renderScannerGrid(window._scannerStats);", interval_replace)

    with open(filepath, 'w') as f:
        f.write(content)

base_dir = "/Users/abhinavmaheshwari/Documents/ELITE_BREAKOUT_SYSTEM/app"
patch_file(os.path.join(base_dir, "user_dashboard.html"))
patch_file(os.path.join(base_dir, "wealth_dashboard.html"))
patch_file(os.path.join(base_dir, "admin_dashboard.html"))
print("Done")
