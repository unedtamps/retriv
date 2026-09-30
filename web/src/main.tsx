import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/fira-sans/latin-400.css'
import '@fontsource/fira-sans/latin-500.css'
import '@fontsource/fira-sans/latin-600.css'
import '@fontsource/fira-code/latin-500.css'
import './index.css'
import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
