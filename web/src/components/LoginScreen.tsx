import { useState, type FormEvent } from 'react'
import { ArrowRight, KeyRound, LockKeyhole } from 'lucide-react'
import { getErrorMessage } from '../api'
import { BrandMark } from './ui/Brand'
import loginScene from '../assets/login-scene.png'

interface LoginScreenProps {
  onLogin: (username: string, password: string) => Promise<void>
}

/**
 * The sign-in page.
 *
 * This is the one screen that keeps a dark panel. Everywhere else the app
 * is light, so the sign-in page is the only place a brand surface appears,
 * and it is the only place one earns its keep.
 */
export function LoginScreen({ onLogin }: LoginScreenProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [status, setStatus] = useState<'idle' | 'submitting'>('idle')
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setStatus('submitting')
    setError(null)
    try {
      await onLogin(username, password)
    } catch (caught) {
      setError(getErrorMessage(caught))
      setStatus('idle')
    }
  }

  return (
    <main className="min-h-svh">
      <div className="grid min-h-svh lg:grid-cols-[0.9fr_1.1fr]">
        <section className="relative hidden overflow-hidden bg-panel-dark text-on-dark lg:flex">
          <img
            src={loginScene}
            alt=""
            aria-hidden="true"
            className="absolute inset-0 h-full w-full object-cover opacity-90"
          />
          <div
            aria-hidden="true"
            className="absolute inset-0 bg-gradient-to-b from-panel-dark/95 via-panel-dark/75 to-panel-dark/94"
          />
          <div className="relative flex w-full flex-col justify-between p-8 xl:p-10">
            <div className="flex items-center gap-3">
              <BrandMark />
              <p className="font-condensed text-sm font-bold uppercase tracking-[0.04em]">
                Retriv
              </p>
            </div>

            <div>
              <h1 className="font-condensed text-3xl font-bold uppercase leading-[1.12] tracking-[0.03em] xl:text-4xl">
                Access is explicit
                <br />
                Context stays scoped
              </h1>
              <p className="mt-4 max-w-md text-sm leading-6 text-on-dark-muted">
                Administer users, workspace ownership, and retrieval boundaries from one
                deliberate surface.
              </p>
            </div>

            <ol className="grid grid-cols-3 gap-4 border-t border-on-dark-line pt-6">
              {[
                ['01', 'Identity'],
                ['02', 'Membership'],
                ['03', 'Context'],
              ].map(([number, label]) => (
                <li key={number}>
                  <p className="font-mono text-eyebrow text-on-dark-accent">{number}</p>
                  <p className="mt-1.5 text-sm text-on-dark">{label}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section className="flex items-center justify-center px-5 py-10 sm:px-10">
          <div className="w-full max-w-sm">
            <div className="mb-8 flex items-center gap-3 lg:hidden">
              <BrandMark />
              <p className="font-condensed text-sm font-bold uppercase tracking-[0.04em] text-ink">
                Retriv
              </p>
            </div>

            <div className="flex h-10 w-10 items-center justify-center rounded-stamp border border-line bg-sunken text-ink">
              <LockKeyhole aria-hidden="true" size={19} strokeWidth={1.8} />
            </div>
            <h2 className="mt-4 font-condensed text-base font-bold uppercase tracking-[0.05em] text-ink">
              Sign in to the dashboard
            </h2>
            <p className="mt-2 text-sm leading-6 text-ink-muted">
              Your workspace permissions are resolved from the current access policy after
              sign-in.
            </p>

            <form className="mt-7 space-y-4" onSubmit={handleSubmit}>
              <div>
                <label className="label" htmlFor="username">
                  Username
                </label>
                <input
                  id="username"
                  name="username"
                  className="field"
                  value={username}
                  autoComplete="username"
                  autoFocus
                  required
                  maxLength={255}
                  onChange={(event) => setUsername(event.target.value)}
                />
              </div>
              <div>
                <label className="label" htmlFor="password">
                  Password
                </label>
                <div className="relative">
                  <KeyRound
                    aria-hidden="true"
                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint"
                    size={16}
                  />
                  <input
                    id="password"
                    name="password"
                    type="password"
                    className="field pl-9"
                    value={password}
                    autoComplete="current-password"
                    required
                    onChange={(event) => setPassword(event.target.value)}
                  />
                </div>
              </div>
              {error === null ? null : (
                <p className="error-note" role="alert">
                  {error}
                </p>
              )}
              <button
                type="submit"
                className="btn btn-primary w-full justify-between py-3"
                disabled={status === 'submitting'}
              >
                <span>{status === 'submitting' ? 'Signing in…' : 'Continue'}</span>
                <ArrowRight aria-hidden="true" size={18} />
              </button>
            </form>
            <p className="mt-6 border-t border-line pt-4 font-mono text-[0.65rem] leading-5 text-ink-faint">
              Sessions are kept in this browser tab and cleared when you sign out or close it.
            </p>
          </div>
        </section>
      </div>
    </main>
  )
}
