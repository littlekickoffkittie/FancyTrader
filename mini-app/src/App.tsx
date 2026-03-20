import React, { useEffect, useState } from 'react'
import ConfigTab from './ConfigTab'
import { supabase } from './lib/supabase'
import { LineChart, Line, ResponsiveContainer, Tooltip } from 'recharts'

declare global { interface Window { Telegram: any } }

const tg = window.Telegram?.WebApp
const TG_USER_ID = tg?.initDataUnsafe?.user?.id
  ? `tg-${tg.initDataUnsafe.user.id}`
  : 'c07c8328-8903-44c2-a8ee-fb8bb5432a44'

interface Snapshot {
  equity: number
  delta: number
  win_rate: number
  profit_factor: number
  max_drawdown: number
  total_trades: number
  snapshot_at: string
}

interface Trade {
  id: string
  symbol: string
  direction: string
  entry_price: number
  exit_price: number | null
  pnl_usdt: number | null
  signal_score: number
  mode: string
}

interface Log {
  id: string
  message: string
  level: string
  created_at: string
}

export default function App() {
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [trades, setTrades] = useState<Trade[]>([])
  const [logs, setLogs] = useState<Log[]>([])
  const [tab, setTab] = useState<'dashboard' | 'trades' | 'logs' | 'config'>('dashboard')

  const latest = snapshots[snapshots.length - 1]
  const equityCurve = snapshots.map(s => ({ equity: s.equity }))
  const openTrades = trades.filter(t => t.exit_price === null)
  const closedTrades = trades.filter(t => t.exit_price !== null)

  useEffect(() => {
    tg?.ready()
    tg?.expand()
    fetchAll()

    // Realtime subscriptions
    const sub = supabase
      .channel('dashboard')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'performance_snapshots' },
        () => fetchSnapshots())
      .on('postgres_changes', { event: '*', schema: 'public', table: 'trades' },
        () => fetchTrades())
      .on('postgres_changes', { event: '*', schema: 'public', table: 'execution_logs' },
        () => fetchLogs())
      .subscribe()

    return () => { supabase.removeChannel(sub) }
  }, [])

  async function fetchAll() {
    await Promise.all([fetchSnapshots(), fetchTrades(), fetchLogs()])
  }

  async function fetchSnapshots() {
    const { data } = await supabase
      .from('performance_snapshots')
      .select('*')
      .eq('user_id', TG_USER_ID)
      .order('snapshot_at', { ascending: true })
      .limit(100)
    if (data) setSnapshots(data)
  }

  async function fetchTrades() {
    const { data } = await supabase
      .from('trades')
      .select('*')
      .eq('user_id', TG_USER_ID)
      .order('entry_at', { ascending: false })
      .limit(50)
    if (data) setTrades(data)
  }

  async function fetchLogs() {
    const { data } = await supabase
      .from('execution_logs')
      .select('*')
      .eq('user_id', TG_USER_ID)
      .order('created_at', { ascending: false })
      .limit(30)
    if (data) setLogs(data)
  }

  const s: React.CSSProperties = {
    background: '#0d0d0d',
    color: '#e0e0e0',
    minHeight: '100vh',
    fontFamily: 'monospace',
    fontSize: 13,
    padding: '0 0 80px 0'
  }

  const card: React.CSSProperties = {
    background: '#1a1a1a',
    borderRadius: 8,
    padding: '12px 16px',
    margin: '8px 12px'
  }

  const green = '#00e676'
  const red = '#ff1744'
  const muted = '#666'

  return (
    <div style={s}>
      {/* Header */}
      <div style={{ padding: '16px 16px 8px', borderBottom: '1px solid #222' }}>
        <div style={{ fontWeight: 'bold', fontSize: 15, letterSpacing: 1 }}>
          FANCYBOT <span style={{ color: muted, fontSize: 11 }}>v2.0</span>
        </div>
        {latest && (
          <div style={{ display: 'flex', gap: 24, marginTop: 8 }}>
            <div>
              <div style={{ color: muted, fontSize: 10 }}>EQUITY</div>
              <div style={{ color: green, fontSize: 18, fontWeight: 'bold' }}>
                ${latest.equity.toFixed(2)}
              </div>
            </div>
            <div>
              <div style={{ color: muted, fontSize: 10 }}>DELTA</div>
              <div style={{ color: latest.delta >= 0 ? green : red, fontSize: 18 }}>
                {latest.delta >= 0 ? '+' : ''}{latest.delta?.toFixed(2)}
              </div>
            </div>
            <div>
              <div style={{ color: muted, fontSize: 10 }}>WIN%</div>
              <div style={{ fontSize: 18 }}>{latest.win_rate}%</div>
            </div>
            <div>
              <div style={{ color: muted, fontSize: 10 }}>MAX DD</div>
              <div style={{ color: red, fontSize: 18 }}>{latest.max_drawdown}%</div>
            </div>
          </div>
        )}
      </div>

      {/* Equity Curve */}
      {equityCurve.length > 1 && (
        <div style={{ ...card, padding: '8px 4px' }}>
          <ResponsiveContainer width="100%" height={80}>
            <LineChart data={equityCurve}>
              <Line type="monotone" dataKey="equity" stroke={green}
                    dot={false} strokeWidth={2} />
              <Tooltip
                contentStyle={{ background: '#1a1a1a', border: 'none', fontSize: 11 }}
                formatter={(v: any) => [`$${Number(v).toFixed(2)}`, 'Equity']}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Tabs */}
      <div style={{ display: 'flex', borderBottom: '1px solid #222', margin: '8px 0 0' }}>
        {(['dashboard', 'trades', 'logs', 'config'] as const).map(t => (
          <div key={t} onClick={() => setTab(t)} style={{
            flex: 1, textAlign: 'center', padding: '10px 0',
            color: tab === t ? green : muted,
            borderBottom: tab === t ? `2px solid ${green}` : '2px solid transparent',
            cursor: 'pointer', fontSize: 11, textTransform: 'uppercase', letterSpacing: 1
          }}>{t}</div>
        ))}
      </div>

      {/* Dashboard Tab */}
      {tab === 'dashboard' && (
        <div>
          {/* Stats */}
          {latest && (
            <div style={card}>
              <div style={{ color: muted, fontSize: 10, marginBottom: 8 }}>STATISTICS</div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                {[
                  ['Total Trades', latest.total_trades],
                  ['Profit Factor', latest.profit_factor?.toFixed(3)],
                  ['Win Rate', `${latest.win_rate}%`],
                  ['Max Drawdown', `${latest.max_drawdown}%`],
                ].map(([k, v]) => (
                  <div key={String(k)}>
                    <div style={{ color: muted, fontSize: 10 }}>{k}</div>
                    <div style={{ fontSize: 14 }}>{v}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Open Positions */}
          <div style={card}>
            <div style={{ color: muted, fontSize: 10, marginBottom: 8 }}>
              OPEN POSITIONS — {openTrades.length} ACTIVE
            </div>
            {openTrades.length === 0 && (
              <div style={{ color: muted }}>No open positions</div>
            )}
            {openTrades.map(t => (
              <div key={t.id} style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '6px 0', borderBottom: '1px solid #222'
              }}>
                <div>
                  <span style={{
                    background: t.direction === 'long' ? '#003300' : '#330000',
                    color: t.direction === 'long' ? green : red,
                    padding: '2px 6px', borderRadius: 4, fontSize: 10, marginRight: 8
                  }}>
                    {t.direction.toUpperCase()}
                  </span>
                  {t.symbol}
                </div>
                <div style={{ color: muted }}> @ {t.entry_price}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Trades Tab */}
      {tab === 'trades' && (
        <div style={card}>
          <div style={{ color: muted, fontSize: 10, marginBottom: 8 }}>TRADE HISTORY</div>
          {closedTrades.length === 0 && (
            <div style={{ color: muted }}>No closed trades yet</div>
          )}
          {closedTrades.map(t => (
            <div key={t.id} style={{
              display: 'flex', justifyContent: 'space-between',
              padding: '6px 0', borderBottom: '1px solid #222'
            }}>
              <div>
                <span style={{
                  color: t.direction === 'long' ? green : red,
                  marginRight: 8, fontSize: 10
                }}>
                  {t.direction.toUpperCase()}
                </span>
                {t.symbol}
                <span style={{ color: muted, fontSize: 10, marginLeft: 8 }}>
                  {t.mode}
                </span>
              </div>
              <div style={{ color: (t.pnl_usdt ?? 0) >= 0 ? green : red }}>
                {(t.pnl_usdt ?? 0) >= 0 ? '+' : ''}{t.pnl_usdt?.toFixed(2)} USDT
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Logs Tab */}
      {tab === 'logs' && (
        <div style={card}>
          <div style={{ color: muted, fontSize: 10, marginBottom: 8 }}>SYSTEM LOG</div>
          {logs.map(l => (
            <div key={l.id} style={{
              padding: '4px 0', borderBottom: '1px solid #111',
              color: l.level === 'error' ? red : l.level === 'audit' ? '#64b5f6' : muted,
              fontSize: 11
            }}>
              {l.message}
            </div>
          ))}
        </div>
      )}

      {/* Config Tab */}
      {tab === 'config' && (
        <ConfigTab userId={TG_USER_ID} />
      )}
    </div>
  )
}