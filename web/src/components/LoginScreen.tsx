import { useState, type FormEvent } from 'react'
import { ArrowRight, KeyRound, Layers3, LockKeyhole } from 'lucide-react'
import { getErrorMessage } from '../api'

interface LoginScreenProps {
  onLogin: (username: string, password: string) => Promise<void>
}

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
    <main className="min-h-svh bg-[#f4f1e8] p-3 sm:p-5 lg:p-7">
      <div className="mx-auto grid min-h-[calc(100svh-1.5rem)] max-w-[92rem] overflow-hidden border border-stone-300 bg-white shadow-[0_24px_80px_rgba(28,36,31,0.10)] sm:min-h-[calc(100svh-2.5rem)] lg:grid-cols-[1.15fr_0.85fr]">
        <section className="relative hidden overflow-hidden bg-[#17231f] p-10 text-stone-50 lg:flex lg:flex-col lg:justify-between xl:p-14">
          <div className="absolute inset-y-0 right-0 w-px bg-white/10" />
          <div className="absolute -right-32 top-20 h-80 w-80 rounded-full border border-lime-200/20" />
          <div className="absolute -right-14 top-48 h-48 w-48 rounded-full border border-lime-200/20" />
          <Brand light />
          <div className="relative max-w-2xl">
            <p className="mb-5 font-mono text-xs font-semibold uppercase tracking-[0.22em] text-[#d9f15f]">
              Workspace control plane
            </p>
            <h1 className="text-5xl font-semibold leading-[1.03] tracking-[-0.045em] xl:text-6xl">
              Access is explicit.
              <br />
              Context stays scoped.
            </h1>
            <p className="mt-6 max-w-xl text-base leading-7 text-stone-300">
              Administer users, workspace ownership, and retrieval boundaries from one deliberate
              surface.
            </p>
          </div>
          <div className="relative grid grid-cols-3 gap-px border border-white/15 bg-white/15">
            {[
              ['01', 'Identity'],
              ['02', 'Membership'],
              ['03', 'Context'],
            ].map(([number, label]) => (
              <div key={number} className="bg-[#17231f] px-4 py-4">
                <p className="font-mono text-[0.65rem] text-[#d9f15f]">{number}</p>
                <p className="mt-2 text-sm text-stone-200">{label}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="flex items-center justify-center px-5 py-10 sm:px-10 lg:px-14">
          <div className="w-full max-w-md">
            <div className="mb-12 lg:hidden">
              <Brand />
            </div>
            <div className="mb-8 flex h-11 w-11 items-center justify-center border border-emerald-900/20 bg-emerald-50 text-emerald-950">
              <LockKeyhole aria-hidden="true" size={20} strokeWidth={1.8} />
            </div>
            <p className="font-mono text-[0.68rem] font-semibold uppercase tracking-[0.2em] text-emerald-800">
              Authorized access only
            </p>
            <h2 className="mt-3 text-3xl font-semibold tracking-[-0.035em] text-stone-950">
              Sign in to the dashboard
            </h2>
            <p className="mt-3 text-sm leading-6 text-stone-600">
              Your workspace permissions are resolved from the current access policy after sign-in.
            </p>

            <form className="mt-8 space-y-5" onSubmit={handleSubmit}>
              <div>
                <label className="field-label" htmlFor="username">
                  Username
                </label>
                <input
                  id="username"
                  name="username"
                  className="field-input"
                  value={username}
                  autoComplete="username"
                  autoFocus
                  required
                  maxLength={255}
                  onChange={(event) => setUsername(event.target.value)}
                />
              </div>
              <div>
                <label className="field-label" htmlFor="password">
                  Password
                </label>
                <div className="relative">
                  <KeyRound
                    aria-hidden="true"
                    className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-stone-400"
                    size={17}
                  />
                  <input
                    id="password"
                    name="password"
                    type="password"
                    className="field-input pl-11"
                    value={password}
                    autoComplete="current-password"
                    required
                    onChange={(event) => setPassword(event.target.value)}
                  />
                </div>
              </div>
              {error === null ? null : (
                <div className="border-l-2 border-red-700 bg-red-50 px-4 py-3 text-sm leading-6 text-red-900" role="alert">
                  {error}
                </div>
              )}
              <button
                type="submit"
                className="button-primary w-full justify-between py-3.5"
                disabled={status === 'submitting'}
              >
                <span>{status === 'submitting' ? 'Signing in…' : 'Continue'}</span>
                <ArrowRight aria-hidden="true" size={18} />
              </button>
            </form>
            <p className="mt-8 border-t border-stone-200 pt-5 text-xs leading-5 text-stone-500">
              Sessions are kept in this browser tab and cleared when you sign out or close it.
            </p>
          </div>
        </section>
      </div>
    </main>
  )
}

interface BrandProps {
  light?: boolean
}

export function Brand({ light = false }: BrandProps) {
  return (
    <div className="relative flex items-center gap-3">
      <span
        className={
          light
            ? 'flex h-9 w-9 items-center justify-center bg-[#d9f15f] text-[#17231f]'
            : 'flex h-9 w-9 items-center justify-center bg-[#17231f] text-[#f4f1e8]'
        }
      >
        <Layers3 aria-hidden="true" size={18} strokeWidth={2} />
      </span>
      <span>
        <span className={light ? 'block text-base font-semibold text-white' : 'block text-base font-semibold text-stone-950'}>
          Retriv
        </span>
        <span className={light ? 'block font-mono text-[0.58rem] uppercase tracking-[0.22em] text-stone-400' : 'block font-mono text-[0.58rem] uppercase tracking-[0.22em] text-stone-500'}>
          Control plane
        </span>
      </span>
    </div>
  )
}
