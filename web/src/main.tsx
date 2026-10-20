import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { captureTokenFromFragment } from './auth'

// A `#token=...` fragment (auth-enabled server) is moved into sessionStorage
// and stripped from the address bar before anything renders or fetches.
captureTokenFromFragment()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
