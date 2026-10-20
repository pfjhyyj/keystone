const { chromium } = require('../frontend/console/node_modules/@playwright/test')

;(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.MEDIA_CHANNEL ? { channel: process.env.MEDIA_CHANNEL } : {}),
  })
  try {
    const context = await browser.newContext()
    const url = new URL(process.env.MEDIA_URL)
    await context.addCookies([{
      name: 'keystone_session', value: process.env.MEDIA_COOKIE, url: url.origin,
      httpOnly: true, sameSite: 'Lax',
    }])
    const page = await context.newPage()
    await page.goto(url.origin + '/api/v1/health')
    const ranges = []
    page.on('response', response => {
      if (response.url() === url.href) ranges.push(response.status())
    })
    const result = await page.evaluate(async source => {
      document.body.innerHTML = '<video controls muted width="640"></video>'
      const video = document.querySelector('video')
      function event(name) {
        return new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('Timed out: ' + name)), 15000)
          video.addEventListener(name, () => { clearTimeout(timer); resolve() }, { once: true })
          video.addEventListener('error', () => reject(new Error('Video decode failed')), { once: true })
        })
      }
      const loaded = event('loadedmetadata')
      video.src = source
      await loaded
      await video.play()
      await event('timeupdate')
      const seeked = event('seeked')
      video.currentTime = 6
      await seeked
      video.pause()
      return { duration: video.duration, seekedTo: video.currentTime, width: video.videoWidth }
    }, url.href)
    if (result.duration < 7 || result.seekedTo < 5.5 || result.width !== 640 || !ranges.includes(206)) {
      throw new Error(JSON.stringify({ result, ranges }))
    }
    console.log(JSON.stringify({ ...result, responseStatuses: ranges }))
  } finally {
    await browser.close()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
