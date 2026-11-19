;(() => {
  const stored = localStorage.getItem('retriv.theme')
  let dark
  if (stored === 'dark' || stored === 'light') {
    dark = stored === 'dark'
  } else {
    dark = window.matchMedia('(prefers-color-scheme: dark)').matches
  }
  const theme = dark ? 'dark' : 'light'
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
  document
    .querySelector('meta[name="theme-color"]')
    .setAttribute('content', dark ? '#0B1017' : '#E4EAF1')
})()
