import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/inter/latin-400.css'
import '@fontsource/inter/latin-500.css'
import '@fontsource/inter/latin-600.css'
import '@fontsource/fira-code/latin-500.css'
import '@fontsource/newsreader/latin-400.css'
import '@fontsource/newsreader/latin-500.css'
import '@fontsource/newsreader/latin-600.css'
import './index.css'
import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
