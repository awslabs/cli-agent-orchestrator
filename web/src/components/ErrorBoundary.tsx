import React from 'react'
import { AlertTriangle } from 'lucide-react'

interface State { hasError: boolean; error: Error | null }

export class ErrorBoundary extends React.Component<{ children: React.ReactNode }, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center py-12 text-gray-400">
          <AlertTriangle size={32} className="text-amber-500 mb-3" />
          <p className="text-sm mb-2">Something went wrong</p>
          <button
            onClick={() => this.setState({ hasError: false, error: null })}
            className="text-xs text-emerald-400 hover:text-emerald-300"
          >
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
