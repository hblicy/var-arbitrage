import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';

const EXCHANGES = ['variational', 'binance', 'nado', 'hyperliquid', 'edgex', 'lighter', 'backpack', 'grvt'];

const formatExchangeName = (name) => name.charAt(0).toUpperCase() + name.slice(1);
const getAuthHeader = (u, p) => `Basic ${btoa(`${u}:${p}`)}`;
const fmtUsd = (v) => {
    if (v === null || v === undefined) return '-';
    const sign = v >= 0 ? '+' : '';
    return `${sign}$${v.toFixed(2)}`;
};
const pnlClass = (v) => {
    if (v === null || v === undefined || v === 0) return '';
    return v > 0 ? 'pnl-positive' : 'pnl-negative';
};
const fmtDays = (hours) => {
    if (!hours) return '-';
    if (hours < 24) return `${hours.toFixed(1)}h`;
    return `${(hours / 24).toFixed(1)}d`;
};
const fmtPrice = (v) => {
    if (!v) return '-';
    if (v >= 100) return `$${v.toFixed(2)}`;
    if (v >= 1) return `$${v.toFixed(4)}`;
    return `$${v.toFixed(6)}`;
};

const PositionsModal = ({ isOpen, onClose }) => {
    const [username, setUsername] = useState('admin');
    const [password, setPassword] = useState('');
    const [isLoggedIn, setIsLoggedIn] = useState(false);
    const [positions, setPositions] = useState({});
    const [newSymbol, setNewSymbol] = useState('');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const refreshTimer = useRef(null);
    const authRef = useRef({ username: 'admin', password: '' });

    const [longEx, setLongEx] = useState('variational');
    const [shortEx, setShortEx] = useState('binance');
    const [entryQty, setEntryQty] = useState('');
    const [entryPriceLong, setEntryPriceLong] = useState('');
    const [entryPriceShort, setEntryPriceShort] = useState('');

    useEffect(() => {
        authRef.current = { username, password };
    }, [username, password]);

    const fetchPositions = useCallback(async (u, p) => {
        const authUser = u ?? authRef.current.username;
        const authPass = p ?? authRef.current.password;

        setLoading(true);
        setError('');
        try {
            const res = await fetch('/api/positions/pnl', {
                headers: { 'Authorization': getAuthHeader(authUser, authPass) }
            });
            if (res.status === 401) {
                setIsLoggedIn(false);
                throw new Error('Invalid credentials');
            }
            if (!res.ok) throw new Error('Fetch failed');

            const data = await res.json();
            setPositions(data);
            setIsLoggedIn(true);
            localStorage.setItem('pos_auth', JSON.stringify({ user: authUser, pass: authPass }));
        } catch (err) {
            console.error(err);
            if (err.message === 'Invalid credentials') setError('Login failed');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        if (!isOpen) return;

        const savedAuth = localStorage.getItem('pos_auth');
        if (savedAuth) {
            const { user, pass } = JSON.parse(savedAuth);
            setUsername(user);
            setPassword(pass);
            authRef.current = { username: user, password: pass };
            fetchPositions(user, pass);
        }
    }, [isOpen, fetchPositions]);

    useEffect(() => {
        if (isOpen && isLoggedIn) {
            refreshTimer.current = window.setInterval(() => {
                fetchPositions();
            }, 30000);
        }
        return () => {
            if (refreshTimer.current) window.clearInterval(refreshTimer.current);
        };
    }, [isOpen, isLoggedIn, fetchPositions]);

    const handleLogin = (e) => {
        e.preventDefault();
        fetchPositions();
    };

    const handleAdd = async (e) => {
        e.preventDefault();
        if (!newSymbol) return;

        let formattedSymbol = newSymbol.toUpperCase().trim();
        if (!formattedSymbol.endsWith('USDT')) {
            formattedSymbol += 'USDT';
        }

        setLoading(true);
        try {
            const { username: authUser, password: authPass } = authRef.current;
            const res = await fetch('/api/positions', {
                method: 'POST',
                headers: {
                    'Authorization': getAuthHeader(authUser, authPass),
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    symbol: formattedSymbol,
                    long_exchange: longEx,
                    short_exchange: shortEx,
                    entry_qty: parseFloat(entryQty) || 0,
                    entry_price_long: parseFloat(entryPriceLong) || 0,
                    entry_price_short: parseFloat(entryPriceShort) || 0
                })
            });

            if (res.ok) {
                setNewSymbol('');
                setEntryQty('');
                setEntryPriceLong('');
                setEntryPriceShort('');
                fetchPositions();
            } else {
                setError('Failed to add position');
            }
        } catch {
            setError('Network error');
        } finally {
            setLoading(false);
        }
    };

    const handleDelete = async (symbol) => {
        if (!confirm(`Stop monitoring ${symbol}?`)) return;

        setLoading(true);
        try {
            const { username: authUser, password: authPass } = authRef.current;
            await fetch(`/api/positions/${symbol}`, {
                method: 'DELETE',
                headers: { 'Authorization': getAuthHeader(authUser, authPass) }
            });
            fetchPositions();
        } catch {
            setError('Delete failed');
        } finally {
            setLoading(false);
        }
    };

    const handleLogout = () => {
        localStorage.removeItem('pos_auth');
        setIsLoggedIn(false);
        setPassword('');
        setPositions({});
    };

    const entries = useMemo(() => Object.entries(positions), [positions]);
    const totals = useMemo(() => entries.reduce((acc, [, d]) => {
        const pnl = d.pnl || {};
        acc.notional += pnl.notional || 0;
        acc.funding += pnl.funding_income || 0;
        acc.fees += pnl.total_fees || 0;
        acc.netPnl += pnl.net_pnl || 0;
        acc.mtm += pnl.mtm_pnl || 0;
        acc.combined += pnl.total_pnl || 0;
        return acc;
    }, {
        notional: 0,
        funding: 0,
        fees: 0,
        netPnl: 0,
        mtm: 0,
        combined: 0,
    }), [entries]);

    if (!isOpen) return null;

    return (
        <div className="modal-overlay">
            <div className="modal-content modal-wide">
                <div className="modal-header">
                    <h2>Position Manager</h2>
                    <button className="close-btn" onClick={onClose}>&times;</button>
                </div>

                <div className="modal-body">
                    {!isLoggedIn ? (
                        <form onSubmit={handleLogin} className="login-form">
                            <div className="form-group">
                                <label>Username</label>
                                <input
                                    type="text"
                                    value={username}
                                    onChange={e => setUsername(e.target.value)}
                                />
                            </div>
                            <div className="form-group">
                                <label>Password</label>
                                <input
                                    type="password"
                                    value={password}
                                    onChange={e => setPassword(e.target.value)}
                                />
                            </div>
                            {error && <div className="error-msg">{error}</div>}
                            <button type="submit" disabled={loading}>Login</button>
                        </form>
                    ) : (
                        <div className="manager-view">
                            <div className="actions-bar">
                                <span className="user-badge">Logged in as {username}</span>
                                <button className="refresh-btn" onClick={() => fetchPositions()} disabled={loading}>
                                    {loading ? 'Loading' : 'Refresh'}
                                </button>
                                <button className="logout-btn" onClick={handleLogout}>Logout</button>
                            </div>

                            <div className="add-pos-section">
                                <h3>Add New Position</h3>
                                <form onSubmit={handleAdd} className="add-form">
                                    <input
                                        type="text"
                                        placeholder="Symbol"
                                        value={newSymbol}
                                        onChange={e => setNewSymbol(e.target.value)}
                                        required
                                    />
                                    <select value={longEx} onChange={e => setLongEx(e.target.value)}>
                                        {EXCHANGES.map(ex => (
                                            <option key={ex} value={ex}>{formatExchangeName(ex)} (Long)</option>
                                        ))}
                                    </select>
                                    <select value={shortEx} onChange={e => setShortEx(e.target.value)}>
                                        {EXCHANGES.map(ex => (
                                            <option key={ex} value={ex}>{formatExchangeName(ex)} (Short)</option>
                                        ))}
                                    </select>
                                    <input
                                        type="number"
                                        placeholder="Qty"
                                        value={entryQty}
                                        onChange={e => setEntryQty(e.target.value)}
                                        step="any"
                                        min="0"
                                    />
                                    <input
                                        type="number"
                                        placeholder="P-Long"
                                        value={entryPriceLong}
                                        onChange={e => setEntryPriceLong(e.target.value)}
                                        step="any"
                                        min="0"
                                    />
                                    <input
                                        type="number"
                                        placeholder="P-Short"
                                        value={entryPriceShort}
                                        onChange={e => setEntryPriceShort(e.target.value)}
                                        step="any"
                                        min="0"
                                    />
                                    <button type="submit" disabled={loading}>+ Monitor</button>
                                </form>
                            </div>

                            <div className="pos-list">
                                <h3>Active Positions ({entries.length})</h3>
                                <div className="list-container pnl-table-scroll">
                                    {entries.length === 0 ? (
                                        <p className="empty-hint">No active positions tracked.</p>
                                    ) : (
                                        <table className="pos-table pnl-table">
                                            <thead>
                                                <tr>
                                                    <th>Symbol</th>
                                                    <th>Direction</th>
                                                    <th className="num-col">Notional</th>
                                                    <th className="num-col">Cur Price</th>
                                                    <th className="num-col">Funding</th>
                                                    <th className="num-col">Fees</th>
                                                    <th className="num-col">Funding PnL</th>
                                                    <th className="num-col">MtM PnL</th>
                                                    <th className="num-col">Total PnL</th>
                                                    <th className="num-col">Days</th>
                                                    <th>Action</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {entries.map(([sym, item]) => {
                                                    const p = item.pnl || {};
                                                    return (
                                                        <tr key={sym}>
                                                            <td className="symbol-cell">{item.symbol || sym.split('_')[0]}</td>
                                                            <td>
                                                                <span className="dir-tag long">{item.long_exchange}</span>
                                                                <span className="dir-arrow">-&gt;</span>
                                                                <span className="dir-tag short">{item.short_exchange}</span>
                                                            </td>
                                                            <td className="num-col">${(p.notional || 0).toFixed(0)}</td>
                                                            <td className="num-col price-cell">
                                                                <span className="price-long">{fmtPrice(item.current_price_long)}</span>
                                                                <span className="price-sep">/</span>
                                                                <span className="price-short">{fmtPrice(item.current_price_short)}</span>
                                                            </td>
                                                            <td className={`num-col ${pnlClass(p.funding_income)}`}>
                                                                {fmtUsd(p.funding_income)}
                                                            </td>
                                                            <td className="num-col pnl-negative">
                                                                -${(p.total_fees || 0).toFixed(2)}
                                                            </td>
                                                            <td className={`num-col ${pnlClass(p.net_pnl)}`}>
                                                                {fmtUsd(p.net_pnl)}
                                                            </td>
                                                            <td className={`num-col ${pnlClass(p.mtm_pnl)}`}>
                                                                {fmtUsd(p.mtm_pnl)}
                                                            </td>
                                                            <td className={`num-col font-bold ${pnlClass(p.total_pnl)}`}>
                                                                {fmtUsd(p.total_pnl)}
                                                            </td>
                                                            <td className="num-col">{fmtDays(p.hours_held)}</td>
                                                            <td>
                                                                <button
                                                                    className="delete-btn"
                                                                    onClick={() => handleDelete(sym)}
                                                                >
                                                                    Close
                                                                </button>
                                                            </td>
                                                        </tr>
                                                    );
                                                })}
                                            </tbody>
                                            <tfoot>
                                                <tr className="summary-row">
                                                    <td colSpan="2"><strong>Total ({entries.length} positions)</strong></td>
                                                    <td className="num-col"><strong>${totals.notional.toFixed(0)}</strong></td>
                                                    <td className="num-col"></td>
                                                    <td className={`num-col ${pnlClass(totals.funding)}`}>
                                                        <strong>{fmtUsd(totals.funding)}</strong>
                                                    </td>
                                                    <td className="num-col pnl-negative">
                                                        <strong>-${totals.fees.toFixed(2)}</strong>
                                                    </td>
                                                    <td className={`num-col ${pnlClass(totals.netPnl)}`}>
                                                        <strong>{fmtUsd(totals.netPnl)}</strong>
                                                    </td>
                                                    <td className={`num-col ${pnlClass(totals.mtm)}`}>
                                                        <strong>{fmtUsd(totals.mtm)}</strong>
                                                    </td>
                                                    <td className={`num-col font-bold ${pnlClass(totals.combined)}`}>
                                                        <strong>{fmtUsd(totals.combined)}</strong>
                                                    </td>
                                                    <td colSpan="2"></td>
                                                </tr>
                                            </tfoot>
                                        </table>
                                    )}
                                </div>
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};

export default PositionsModal;
