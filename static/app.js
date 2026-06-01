// Auto-poll job status every 5s while a run is in progress
// Reloads the page when the run completes so the digest updates automatically
(function () {
  const banner = document.querySelector('[data-running]');
  if (!banner) return;

  const poll = setInterval(async () => {
    try {
      const res  = await fetch('/job-status');
      const data = await res.json();
      if (!data.running) {
        clearInterval(poll);
        window.location.reload();
      }
    } catch (e) {
      clearInterval(poll);
    }
  }, 5000);
})();