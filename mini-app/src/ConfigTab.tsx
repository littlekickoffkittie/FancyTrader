import React, { useEffect, useState } from 'react'
import { supabase } from './lib/supabase'

interface Config {
  mode: string
  signal: {
    score_gate: number
    score_gap_min: number
    rsi_period: number
    rsi_overbought: number
    rsi_oversold: number
    momentum_lookback: number
  }
  risk: {
    kelly_fraction: number
    max_positions: number
    trade_size_usdt: number
    leverage: number
    stop_loss_atr_multiplier: number
    take_profit_ratio: number
    max_drawdown_pct: number
  }
  execution: {
    allow_long: boolean
    allow_short: boolean
    cooldown_seconds: number
    dry_run: boolean
    time_filter_enabled: boolean
    time_filter_utc_start: number
    time_filter_utc_end: number
  }
  scanner: {
    scan_interval_seconds: number
    min_volume_usdt: number
    max_spread_pct: number
    symbol_whitelist: string[]
    symbol_blacklist: string[]
  }
}

const DEFAULT: Config = {
  mode: 'sim',
  signal: {
    score_gate: 60, score_gap_min: 30, rsi_period: 14,
    rsi_overbought: 70, rsi_oversold: 30, momentum_lookback: 20
  },
  risk: {
    kelly_fraction: 0.5, max_positions: 5, trade_size_usdt: 10,
    leverage: 20, stop_loss_atr_multiplier: 1.5,
    take_profit_ratio: 2.0, max_drawdown_pct: 20
  },
  execution: {
    allow_long: true, allow_short: true,
    cooldown_seconds: 86400, dry_run: true,
    time_filter_enabled: false,
    time_filter_utc_start: 0, time_filter_utc_end: 23
  },
  scanner: {
    scan_interval_seconds: 30, min_volume_usdt: 500000,
    max_spread_pct: 0.1, symbol_whitelist: [], symbol_blacklist: []
  }
}

const green = '#00e676'
const red = '#ff1744'
const muted = '#666'
const cardStyle: React.CSSProperties = {
  background: '#1a1a1a', borderRadius: 8,
  padding: '12px 16px', margin: '8px 12px'
}
const labelStyle: React.CSSProperties = { color: muted, fontSize: 10, marginBottom: 2 }
const inputStyle: React.CSSProperties = {
  background: '#111', border: '1px solid #333', borderRadius: 4,
  color: '#e0e0e0', padding: '6px 8px', width: '100%',
  fontFamily: 'monospace', fontSize: 13, boxSizing: 'border-box'
}
const rowStyle: React.CSSProperties = {
  display: 'flex', justifyContent: 'space-between',
  alignItems: 'center', padding: '6px 0',
  borderBottom: '1px solid #111'
}

interface Props { userId: string }

export default function ConfigTab({ userId }: Props) {
  const [cfg, setCfg] = useState<Config>(DEFAULT)
  const [saved, setSaved] = useState(false)
  const [loading, setLoading] = useState(true)
  const [whitelistInput, setWhitelistInput] = useState('')
  const [blacklistInput, setBlacklistInput] = useState('')

  useEffect(() => { loadConfig() }, [userId])

  async function loadConfig() {
    const { data } = await supabase
      .from('bot_configs')
      .select('*')
      .eq('user_id', userId)
      .order('updated_at', { ascending: false })
      .limit(1)
    if (data && data.length > 0) {
      setCfg(data[0].config as Config)
      const wl = (data[0].config as Config).scanner.symbol_whitelist || []
      const bl = (data[0].config as Config).scanner.symbol_blacklist || []
      setWhitelistInput(wl.join(', '))
      setBlacklistInput(bl.join(', '))
    }
    setLoading(false)
  }

  async function saveConfig() {
    const updated = {
      ...cfg,
      scanner: {
        ...cfg.scanner,
        symbol_whitelist: whitelistInput.split(',').map(s => s.trim().toUpperCase()).filter(Boolean),
        symbol_blacklist: blacklistInput.split(',').map(s => s.trim().toUpperCase()).filter(Boolean),
      }
    }
    await supabase.from('bot_configs').upsert({
      user_id: userId,
      mode: updated.mode,
      config: updated,
      updated_at: new Date().toISOString()
    })
    setCfg(updated)
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  function setSignal(key: keyof Config['signal'], val: number) {
    setCfg(c => ({ ...c, signal: { ...c.signal, [key]: val } }))
  }
  function setRisk(key: keyof Config['risk'], val: number) {
    setCfg(c => ({ ...c, risk: { ...c.risk, [key]: val } }))
  }
  function setExec(key: keyof Config['execution'], val: any) {
    setCfg(c => ({ ...c, execution: { ...c.execution, [key]: val } }))
  }
  function setScanner(key: keyof Config['scanner'], val: any) {
    setCfg(c => ({ ...c, scanner: { ...c.scanner, [key]: val } }))
  }

  if (loading) return <div style={{ color: muted, padding: 16 }}>Loading config...</div>

  return (
    <div style={{ paddingBottom: 24 }}>

      {/* Mode selector */}
      <div style={cardStyle}>
        <div style={{ color: muted, fontSize: 10, marginBottom: 8 }}>ENGINE MODE</div>
        <div style={{ display: 'flex', gap: 8 }}>
          {(['backtest', 'sim', 'live'] as const).map(m => (
            <div key={m} onClick={() => setCfg(c => ({ ...c, mode: m }))} style={{
              flex: 1, textAlign: 'center', padding: '8px 0',
              borderRadius: 4, cursor: 'pointer', fontSize: 11,
              textTransform: 'uppercase', letterSpacing: 1,
              background: cfg.mode === m ? (m === 'live' ? '#330000' : '#001a00') : '#111',
              color: cfg.mode === m ? (m === 'live' ? red : green) : muted,
              border: `1px solid ${cfg.mode === m ? (m === 'live' ? red : green) : '#222'}`
            }}>{m}</div>
          ))}
        </div>
        {cfg.mode === 'live' && (
          <div style={{ color: red, fontSize: 10, marginTop: 8 }}>
            ⚠ LIVE mode executes real orders. Confirm API key is trade-only, no withdrawals.
          </div>
        )}
      </div>

      {/* Signal */}
      <div style={cardStyle}>
        <div style={{ color: muted, fontSize: 10, marginBottom: 8 }}>SIGNAL</div>
        {([
          ['Score Gate', 'score_gate', 0, 100, 1],
          ['Score Gap Min', 'score_gap_min', 0, 100, 1],
          ['RSI Period', 'rsi_period', 2, 50, 1],
          ['RSI Overbought', 'rsi_overbought', 50, 100, 1],
          ['RSI Oversold', 'rsi_oversold', 0, 50, 1],
          ['Momentum Lookback', 'momentum_lookback', 2, 100, 1],
        ] as [string, keyof Config['signal'], number, number, number][]).map(([label, key, min, max, step]) => (
          <div key={key} style={rowStyle}>
            <div style={{ flex: 1 }}>
              <div style={labelStyle}>{label}</div>
              <input
                type="range" min={min} max={max} step={step}
                value={cfg.signal[key] as number}
                onChange={e => setSignal(key, Number(e.target.value))}
                style={{ width: '100%', accentColor: green }}
              />
            </div>
            <div style={{ color: green, minWidth: 40, textAlign: 'right', marginLeft: 12 }}>
              {cfg.signal[key]}
            </div>
          </div>
        ))}
      </div>

      {/* Risk */}
      <div style={cardStyle}>
        <div style={{ color: muted, fontSize: 10, marginBottom: 8 }}>RISK</div>
        {([
          ['Kelly Fraction', 'kelly_fraction', 0.1, 1.0, 0.05],
          ['Max Positions', 'max_positions', 1, 20, 1],
          ['Trade Size (USDT)', 'trade_size_usdt', 1, 500, 1],
          ['Leverage', 'leverage', 1, 100, 1],
          ['SL ATR Multiplier', 'stop_loss_atr_multiplier', 0.5, 5, 0.1],
          ['TP Ratio', 'take_profit_ratio', 0.5, 10, 0.1],
          ['Max Drawdown %', 'max_drawdown_pct', 1, 100, 1],
        ] as [string, keyof Config['risk'], number, number, number][]).map(([label, key, min, max, step]) => (
          <div key={key} style={rowStyle}>
            <div style={{ flex: 1 }}>
              <div style={labelStyle}>{label}</div>
              <input
                type="range" min={min} max={max} step={step}
                value={cfg.risk[key] as number}
                onChange={e => setRisk(key, Number(e.target.value))}
                style={{ width: '100%', accentColor: green }}
              />
            </div>
            <div style={{ color: green, minWidth: 40, textAlign: 'right', marginLeft: 12 }}>
              {cfg.risk[key]}
            </div>
          </div>
        ))}
      </div>

      {/* Execution */}
      <div style={cardStyle}>
        <div style={{ color: muted, fontSize: 10, marginBottom: 8 }}>EXECUTION</div>
        {([
          ['Allow Long', 'allow_long'],
          ['Allow Short', 'allow_short'],
          ['Dry Run', 'dry_run'],
          ['Time Filter', 'time_filter_enabled'],
        ] as [string, keyof Config['execution']][]).map(([label, key]) => (
          <div key={key} style={rowStyle}>
            <div style={labelStyle}>{label}</div>
            <div
              onClick={() => setExec(key, !cfg.execution[key])}
              style={{
                width: 40, height: 22, borderRadius: 11, cursor: 'pointer',
                background: cfg.execution[key] ? green : '#333',
                position: 'relative', transition: 'background 0.2s'
              }}
            >
              <div style={{
                position: 'absolute', top: 3,
                left: cfg.execution[key] ? 20 : 3,
                width: 16, height: 16, borderRadius: 8,
                background: '#fff', transition: 'left 0.2s'
              }} />
            </div>
          </div>
        ))}
        <div style={rowStyle}>
          <div style={{ flex: 1 }}>
            <div style={labelStyle}>Cooldown (seconds)</div>
            <input
              type="number" value={cfg.execution.cooldown_seconds}
              onChange={e => setExec('cooldown_seconds', Number(e.target.value))}
              style={inputStyle}
            />
          </div>
        </div>
      </div>

      {/* Scanner */}
      <div style={cardStyle}>
        <div style={{ color: muted, fontSize: 10, marginBottom: 8 }}>SCANNER</div>
        <div style={rowStyle}>
          <div style={{ flex: 1 }}>
            <div style={labelStyle}>Scan Interval (seconds)</div>
            <input
              type="number" value={cfg.scanner.scan_interval_seconds}
              onChange={e => setScanner('scan_interval_seconds', Number(e.target.value))}
              style={inputStyle}
            />
          </div>
        </div>
        <div style={rowStyle}>
          <div style={{ flex: 1 }}>
            <div style={labelStyle}>Min Volume (USDT)</div>
            <input
              type="number" value={cfg.scanner.min_volume_usdt}
              onChange={e => setScanner('min_volume_usdt', Number(e.target.value))}
              style={inputStyle}
            />
          </div>
        </div>
        <div style={{ padding: '6px 0' }}>
          <div style={labelStyle}>Symbol Whitelist (comma separated)</div>
          <input
            type="text" value={whitelistInput}
            onChange={e => setWhitelistInput(e.target.value)}
            placeholder="BTCUSDT, ETHUSDT, SOLUSDT"
            style={inputStyle}
          />
        </div>
        <div style={{ padding: '6px 0' }}>
          <div style={labelStyle}>Symbol Blacklist (comma separated)</div>
          <input
            type="text" value={blacklistInput}
            onChange={e => setBlacklistInput(e.target.value)}
            placeholder="DOGEUSDT, SHIBUSDT"
            style={inputStyle}
          />
        </div>
      </div>

      {/* Save button */}
      <div style={{ padding: '8px 12px' }}>
        <div onClick={saveConfig} style={{
          background: saved ? '#003300' : green,
          color: saved ? green : '#000',
          padding: '12px 0', borderRadius: 6,
          textAlign: 'center', fontWeight: 'bold',
          cursor: 'pointer', fontSize: 13,
          letterSpacing: 1, transition: 'background 0.3s'
        }}>
          {saved ? '✓ SAVED' : 'SAVE CONFIG'}
        </div>
      </div>

    </div>
  )
}
