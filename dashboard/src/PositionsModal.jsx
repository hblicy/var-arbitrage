import React, { useState, useEffect, useRef, useCallback } from 'react';

const AUTH_KEY = 'pos_auth_v2'; // upgraded key
const REFRESH_MS = 30000;

const PositionsModal = ({ isOpen, onClose }) => {
    // ── Auth state ─────────────────────────────────────────────
    const [username, setUsername] = useState('admin');
    const [password, setPassword] = useState('');
    const [currentUser, setCurrentUser] = useState(null); // { id, username, display_name, role, ... }
    const [isLoggedIn, setIsLoggedIn] = useState(false);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const refreshTimer = useRef(null);
    const autoLoginAttempted = useRef(false);

    // ── Tab state ──────────────────────────────────────────────
    const [tab, setTab] = useState('my-positions'); // 'my-positions' | 'all-positions' | 'users'
    const isAdmin = currentUser?.role === 'admin';

    // ── Position state ─────────────────────────────────────────
    const [positions, setPositions] = useState({});
    const [allUsers, setAllUsers] = useState([]);
    const [filterOwner, setFilterOwner] = useState(''); // owner_id filter for admin view
    const [reversals, setReversals] = useState([]);

    // ── Add-position form ───────────────────────────────────────
    const [newSymbol, setNewSymbol] = useState('');
    const [longEx, setLongEx] = useState('variational');
    const [shortEx, setShortEx] = useState('binance');
    const [entryQty, setEntryQty] = useState('');
    const [entryPriceLong, setEntryPriceLong] = useState('');
    const [entryPriceShort, setEntryPriceShort] = useState('');

    // ── User management form ───────────────────────────────────
    const [userForm, setUserForm] = useState({ username: '', password: '', display_name: '', role: 'trader', wecom_webhook: '', enabled: true });
    const [editingUser, setEditingUser] = useState(null); // user being edited

    // ── Auth helpers ───────────────────────────────────────────
    const getAuthHeader = useCallback((u, p) => 'Basic ' + btoa((u || username) + ':' + (p || password)), [username, password]);

    const handleLogout = useCallback(() => {
        localStorage.removeItem(AUTH_KEY);
        setIsLoggedIn(false);
        setCurrentUser(null);
        setPositions({});
        setReversals([]);
        setAllUsers([]);
        setTab('my-positions');
    }, []);

    // ── Data loading ────────────────────────────────────────────
    const loadPositions = useCallback(async (options = {}) => {
        const requestUser = options.user || currentUser;
        if (!requestUser) return;
        try {
            const params = new URLSearchParams();
            const requestIsAdmin = requestUser.role === 'admin';
            const requestTab = options.tab || tab;
            const requestFilterOwner = options.filterOwner ?? filterOwner;
            const authHeader = options.authHeader || getAuthHeader();

            if (requestIsAdmin && requestTab === 'all-positions' && requestFilterOwner) {
                params.set('all_users', '1');
                params.set('owner', requestFilterOwner);
            } else if (requestIsAdmin && requestTab === 'all-positions') {
                params.set('all_users', '1');
            }

            const url = '/api/positions/pnl' + (params.toString() ? '?' + params : '');
            const res = await fetch(url, { headers: { 'Authorization': authHeader } });
            if (res.status === 401) { handleLogout(); return; }
            if (res.ok) setPositions(await res.json());
        } catch {
            setError('加载持仓失败');
        }
    }, [currentUser, tab, filterOwner, getAuthHeader, handleLogout]);

    const loadReversals = useCallback(async (options = {}) => {
        const requestUser = options.user || currentUser;
        if (!requestUser) return;
        try {
            const params = new URLSearchParams();
            const requestFilterOwner = options.filterOwner ?? filterOwner;
            if (requestUser.role === 'admin') {
                params.set('all_users', '1');
                if (requestFilterOwner) params.set('owner', requestFilterOwner);
            }
            const query = params.toString();
            const url = '/api/positions/reversals' + (query ? '?' + query : '');
            const authHeader = options.authHeader || getAuthHeader();
            const res = await fetch(url, { headers: { 'Authorization': authHeader } });
            if (res.ok) setReversals(await res.json());
        } catch {
            setError('加载反转提醒失败');
        }
    }, [currentUser, filterOwner, getAuthHeader]);

    const loadAllUsers = useCallback(async (options = {}) => {
        try {
            const authHeader = options.authHeader || getAuthHeader();
            const res = await fetch('/api/users', { headers: { 'Authorization': authHeader } });
            if (res.ok) setAllUsers(await res.json());
        } catch {
            setError('加载用户列表失败');
        }
    }, [getAuthHeader]);

    const doLogin = useCallback(async (u, p, save = true) => {
        setLoading(true);
        setError('');
        try {
            const authHeader = getAuthHeader(u, p);
            const res = await fetch('/api/me', { headers: { 'Authorization': authHeader } });
            if (res.status === 401) { setIsLoggedIn(false); setCurrentUser(null); setError('登录失败'); return; }
            if (!res.ok) { setError('请求失败'); return; }
            const user = await res.json();
            setCurrentUser(user);
            setIsLoggedIn(true);
            if (save) localStorage.setItem(AUTH_KEY, JSON.stringify({ u, p }));
            const initialLoads = [
                loadPositions({ user, authHeader }),
                loadReversals({ user, authHeader }),
            ];
            if (user.role === 'admin') {
                initialLoads.push(loadAllUsers({ authHeader }));
            }
            await Promise.all(initialLoads);
        } catch {
            setError('网络错误');
        } finally {
            setLoading(false);
        }
    }, [getAuthHeader, loadPositions, loadReversals, loadAllUsers]);

    const handleLogin = (e) => { e.preventDefault(); doLogin(username, password); };

    // ── Load saved auth on open ────────────────────────────────
    useEffect(() => {
        if (!isOpen) {
            autoLoginAttempted.current = false;
            return;
        }
        if (autoLoginAttempted.current) return;
        autoLoginAttempted.current = true;

        const saved = localStorage.getItem(AUTH_KEY);
        if (saved) {
            try {
                const { u, p } = JSON.parse(saved);
                setUsername(u || 'admin');
                setPassword(p || '');
                if (u && p) doLogin(u, p, false);
            } catch {
                localStorage.removeItem(AUTH_KEY);
            }
        }
    }, [isOpen, doLogin]);

    // ── Auto-refresh ───────────────────────────────────────────
    useEffect(() => {
        if (!isOpen || !isLoggedIn) return;
        refreshTimer.current = setInterval(() => {
            loadPositions();
            loadReversals();
        }, REFRESH_MS);
        return () => clearInterval(refreshTimer.current);
    }, [isOpen, isLoggedIn, loadPositions, loadReversals]);

    useEffect(() => {
        if (isLoggedIn && (tab === 'my-positions' || tab === 'all-positions')) loadPositions();
        if (isLoggedIn && (tab === 'my-positions' || tab === 'all-positions')) loadReversals();
        if (isLoggedIn && isAdmin && tab === 'users') loadAllUsers();
    }, [isLoggedIn, isAdmin, tab, filterOwner, loadPositions, loadReversals, loadAllUsers]);

    // ── Position actions ───────────────────────────────────────
    const handleAdd = async (e) => {
        e.preventDefault();
        if (!newSymbol) return;
        let sym = newSymbol.toUpperCase().trim();
        if (!sym.endsWith('USDT')) sym += 'USDT';
        setLoading(true);
        try {
            const res = await fetch('/api/positions', {
                method: 'POST',
                headers: { 'Authorization': getAuthHeader(), 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    symbol: sym, long_exchange: longEx, short_exchange: shortEx,
                    entry_qty: parseFloat(entryQty) || 0,
                    entry_price_long: parseFloat(entryPriceLong) || 0,
                    entry_price_short: parseFloat(entryPriceShort) || 0,
                }),
            });
            if (res.ok) {
                setNewSymbol(''); setEntryQty(''); setEntryPriceLong(''); setEntryPriceShort('');
                await loadPositions();
                await loadReversals();
            } else { const d = await res.json(); setError(d.detail || 'Failed'); }
        } catch { setError('Network error'); }
        finally { setLoading(false); }
    };

    const handleDelete = async (posId) => {
        if (!confirm('确认平仓/删除此记录？')) return;
        try {
            await fetch(`/api/positions/${encodeURIComponent(posId)}`, {
                method: 'DELETE', headers: { 'Authorization': getAuthHeader() },
            });
            await loadPositions();
            await loadReversals();
        } catch { setError('Delete failed'); }
    };

    // ── User management ─────────────────────────────────────────
    const handleCreateUser = async (e) => {
        e.preventDefault();
        if (!userForm.username || !userForm.password) { setError('用户名和密码必填'); return; }
        setLoading(true);
        try {
            const res = await fetch('/api/users', {
                method: 'POST',
                headers: { 'Authorization': getAuthHeader(), 'Content-Type': 'application/json' },
                body: JSON.stringify(userForm),
            });
            const d = await res.json();
            if (res.ok) {
                setUserForm({ username: '', password: '', display_name: '', role: 'trader', wecom_webhook: '', enabled: true });
                loadAllUsers();
            } else { setError(d.detail || 'Failed'); }
        } catch { setError('Network error'); }
        finally { setLoading(false); }
    };

    const handleDeleteUser = async (userId, username) => {
        if (!confirm(`删除用户 ${username} 及其所有持仓？`)) return;
        try {
            const res = await fetch(`/api/users/${userId}`, { method: 'DELETE', headers: { 'Authorization': getAuthHeader() } });
            if (res.ok) loadAllUsers(); else { const d = await res.json(); setError(d.detail || 'Failed'); }
        } catch { setError('Delete failed'); }
    };

    const startEditUser = (u) => {
        setEditingUser(u);
        setUserForm({
            username: u.username, password: '', display_name: u.display_name || '',
            role: u.role || 'trader', wecom_webhook: u.wecom_webhook || '', enabled: !!u.enabled,
        });
    };

    const handleUpdateUser = async (e) => {
        e.preventDefault();
        setLoading(true);
        try {
            const body = { ...userForm };
            if (!body.password) delete body.password;
            const res = await fetch(`/api/users/${editingUser.id}`, {
                method: 'PUT',
                headers: { 'Authorization': getAuthHeader(), 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const d = await res.json();
            if (res.ok) { setEditingUser(null); setUserForm({ username: '', password: '', display_name: '', role: 'trader', wecom_webhook: '', enabled: true }); loadAllUsers(); }
            else { setError(d.detail || 'Failed'); }
        } catch { setError('Network error'); }
        finally { setLoading(false); }
    };

    // ── Format helpers ─────────────────────────────────────────
    const fmtUsd = (v) => { if (v == null) return '-'; return `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`; };
    const pnlClass = (v) => { if (v == null || v === 0) return ''; return v > 0 ? 'pnl-positive' : 'pnl-negative'; };
    const fmtDays = (h) => { if (!h) return '-'; return h < 24 ? `${h.toFixed(1)}h` : `${(h / 24).toFixed(1)}d`; };
    const fmtPrice = (v) => { if (!v) return '-'; return v >= 100 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`; };

    const healthBadge = (status) => {
        if (!status || status === 'healthy') return <span className="health-badge healthy">正常</span>;
        if (status === 'watch') return <span className="health-badge watch">观察</span>;
        if (status === 'reversal') return <span className="health-badge reversal">反转</span>;
        return null;
    };

    // ── Compute totals ──────────────────────────────────────────
    const entries = Object.entries(positions);
    const totalCombined = entries.reduce((s, [, d]) => s + (d.pnl?.total_pnl || 0), 0);
    const totalNotional = entries.reduce((s, [, d]) => s + (d.pnl?.notional || 0), 0);
    const totalFunding = entries.reduce((s, [, d]) => s + (d.pnl?.funding_income || 0), 0);
    const totalFees = entries.reduce((s, [, d]) => s + (d.pnl?.total_fees || 0), 0);
    const totalMtm = entries.reduce((s, [, d]) => s + (d.pnl?.mtm_pnl || 0), 0);

    // ── Owner map for display ──────────────────────────────────
    const ownerMap = Object.fromEntries(allUsers.map(u => [u.id, u]));

    if (!isOpen) return null;

    return (
        <div className="modal-overlay">
            <div className="modal-content modal-wide">
                <div className="modal-header">
                    <h2>📊 Position Manager</h2>
                    <button className="close-btn" onClick={onClose}>&times;</button>
                </div>

                <div className="modal-body">
                    {!isLoggedIn ? (
                        <form onSubmit={handleLogin} className="login-form">
                            <div className="form-group">
                                <label>Username</label>
                                <input type="text" value={username} onChange={e => setUsername(e.target.value)} />
                            </div>
                            <div className="form-group">
                                <label>Password</label>
                                <input type="password" value={password} onChange={e => setPassword(e.target.value)} />
                            </div>
                            {error && <div className="error-msg">{error}</div>}
                            <button type="submit" disabled={loading}>{loading ? '登录中...' : '登录'}</button>
                        </form>
                    ) : (
                        <div className="manager-view">
                            {/* ── Actions bar ── */}
                            <div className="actions-bar">
                                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                                    <span className="user-badge">
                                        {currentUser?.display_name || currentUser?.username}
                                        {isAdmin && <span className="role-tag">Admin</span>}
                                    </span>
                                    {isAdmin && (
                                        <div className="tab-pills">
                                            <button className={`tab-pill ${tab === 'my-positions' ? 'active' : ''}`} onClick={() => setTab('my-positions')}>我的持仓</button>
                                            <button className={`tab-pill ${tab === 'all-positions' ? 'active' : ''}`} onClick={() => setTab('all-positions')}>全部持仓</button>
                                            <button className={`tab-pill ${tab === 'users' ? 'active' : ''}`} onClick={() => setTab('users')}>用户管理</button>
                                        </div>
                                    )}
                                </div>
                                <div style={{ display: 'flex', gap: '8px' }}>
                                    <button className="refresh-btn" onClick={loadPositions} disabled={loading}>🔄 刷新</button>
                                    <button className="logout-btn" onClick={handleLogout}>退出</button>
                                </div>
                            </div>

                            {/* ── Error banner ── */}
                            {error && <div className="error-msg" style={{ marginBottom: '12px' }}>{error}<button style={{ marginLeft: '12px', background: 'none', border: 'none', color: 'inherit', cursor: 'pointer' }} onClick={() => setError('')}>×</button></div>}

                            {/* ══════════════════════════════════════════════
                                Tab: 我的持仓
                            ══════════════════════════════════════════════ */}
                            {(tab === 'my-positions' || !isAdmin) && (
                                <div>
                                    {/* Reversal alert banner */}
                                    {reversals.length > 0 && (
                                        <div className="reversal-banner">
                                            <span>⚠️ 有 {reversals.length} 个仓位触发反转信号：</span>
                                            {reversals.map(r => (
                                                <span key={r.id} className="reversal-chip">{r.symbol} {r.health_status === 'reversal' ? '🔴' : '🟡'}</span>
                                            ))}
                                        </div>
                                    )}

                                    {/* Add position form */}
                                    <div className="add-pos-section">
                                        <h3>录入新持仓</h3>
                                        <form onSubmit={handleAdd} className="add-form">
                                            <input type="text" placeholder="Symbol" value={newSymbol} onChange={e => setNewSymbol(e.target.value)} required />
                                            <select value={longEx} onChange={e => setLongEx(e.target.value)}>
                                                {['variational','binance','nado','hyperliquid','edgex','lighter','backpack','grvt'].map(ex => (
                                                    <option key={ex} value={ex}>{ex} (Long)</option>
                                                ))}
                                            </select>
                                            <select value={shortEx} onChange={e => setShortEx(e.target.value)}>
                                                {['binance','variational','nado','hyperliquid','edgex','lighter','backpack','grvt'].map(ex => (
                                                    <option key={ex} value={ex}>{ex} (Short)</option>
                                                ))}
                                            </select>
                                            <input type="number" placeholder="Qty" value={entryQty} onChange={e => setEntryQty(e.target.value)} step="any" min="0" />
                                            <input type="number" placeholder="P-Long" value={entryPriceLong} onChange={e => setEntryPriceLong(e.target.value)} step="any" min="0" />
                                            <input type="number" placeholder="P-Short" value={entryPriceShort} onChange={e => setEntryPriceShort(e.target.value)} step="any" min="0" />
                                            <button type="submit" disabled={loading}>+ 录入</button>
                                        </form>
                                    </div>

                                    {/* Positions table */}
                                    <div className="pos-list">
                                        <h3>我的持仓 ({entries.length})</h3>
                                        <div className="list-container pnl-table-scroll">
                                            {entries.length === 0 ? (
                                                <p className="empty-hint">暂无持仓记录</p>
                                            ) : (
                                                <table className="pos-table pnl-table">
                                                    <thead>
                                                        <tr>
                                                            <th>币种</th>
                                                            <th>方向</th>
                                                            <th className="num-col">名义本金</th>
                                                            <th className="num-col">当前价格</th>
                                                            <th className="num-col">资金费</th>
                                                            <th className="num-col">手续费</th>
                                                            <th className="num-col">Funding PnL</th>
                                                            <th className="num-col">MtM PnL</th>
                                                            <th className="num-col">总盈亏</th>
                                                            <th className="num-col">持仓</th>
                                                            <th>状态</th>
                                                            <th>操作</th>
                                                        </tr>
                                                    </thead>
                                                    <tbody>
                                                        {entries.map(([pId, data]) => {
                                                            const p = data.pnl || {};
                                                            return (
                                                                <tr key={pId} className={data.health_status === 'reversal' ? 'row-reversal' : ''}>
                                                                    <td className="symbol-cell">{data.symbol || pId.split('_')[0]}</td>
                                                                    <td>
                                                                        <span className="dir-tag long">{data.long_exchange}</span>
                                                                        <span className="dir-arrow">→</span>
                                                                        <span className="dir-tag short">{data.short_exchange}</span>
                                                                    </td>
                                                                    <td className="num-col">${(p.notional || 0).toFixed(0)}</td>
                                                                    <td className="num-col price-cell">
                                                                        <span className="price-long">{fmtPrice(data.current_price_long)}</span>
                                                                        <span className="price-sep">/</span>
                                                                        <span className="price-short">{fmtPrice(data.current_price_short)}</span>
                                                                    </td>
                                                                    <td className={`num-col ${pnlClass(p.funding_income)}`}>{fmtUsd(p.funding_income)}</td>
                                                                    <td className="num-col pnl-negative">-${(p.total_fees || 0).toFixed(2)}</td>
                                                                    <td className={`num-col ${pnlClass(p.net_pnl)}`}>{fmtUsd(p.net_pnl)}</td>
                                                                    <td className={`num-col ${pnlClass(p.mtm_pnl)}`}>{fmtUsd(p.mtm_pnl)}</td>
                                                                    <td className={`num-col font-bold ${pnlClass(p.total_pnl)}`}>{fmtUsd(p.total_pnl)}</td>
                                                                    <td className="num-col">{fmtDays(p.hours_held)}</td>
                                                                    <td>{healthBadge(data.health_status)}</td>
                                                                    <td><button className="delete-btn" onClick={() => handleDelete(pId)}>平仓</button></td>
                                                                </tr>
                                                            );
                                                        })}
                                                    </tbody>
                                                    <tfoot>
                                                        <tr className="summary-row">
                                                            <td colSpan="2"><strong>合计 ({entries.length} 笔)</strong></td>
                                                            <td className="num-col"><strong>${totalNotional.toFixed(0)}</strong></td>
                                                            <td className="num-col"></td>
                                                            <td className={`num-col ${pnlClass(totalFunding)}`}><strong>{fmtUsd(totalFunding)}</strong></td>
                                                            <td className="num-col pnl-negative"><strong>-${totalFees.toFixed(2)}</strong></td>
                                                            <td className={`num-col ${pnlClass(totalFunding - totalFees)}`}><strong>{fmtUsd(totalFunding - totalFees)}</strong></td>
                                                            <td className={`num-col ${pnlClass(totalMtm)}`}><strong>{fmtUsd(totalMtm)}</strong></td>
                                                            <td className={`num-col font-bold ${pnlClass(totalCombined)}`}><strong>{fmtUsd(totalCombined)}</strong></td>
                                                            <td colSpan="3"></td>
                                                        </tr>
                                                    </tfoot>
                                                </table>
                                            )}
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* ══════════════════════════════════════════════
                                Tab: 全部持仓 (admin only)
                            ══════════════════════════════════════════════ */}
                            {tab === 'all-positions' && isAdmin && (
                                <div>
                                    <div className="filter-bar">
                                        <label>按用户筛选：</label>
                                        <select value={filterOwner} onChange={e => setFilterOwner(e.target.value)}>
                                            <option value="">全部用户</option>
                                            {allUsers.map(u => <option key={u.id} value={u.id}>{u.display_name || u.username}</option>)}
                                        </select>
                                        <button className="refresh-btn" style={{ marginLeft: '8px' }} onClick={loadPositions}>🔄</button>
                                    </div>
                                    <div className="list-container pnl-table-scroll" style={{ marginTop: '12px' }}>
                                        {entries.length === 0 ? (
                                            <p className="empty-hint">暂无持仓记录</p>
                                        ) : (
                                            <table className="pos-table pnl-table">
                                                <thead>
                                                    <tr>
                                                        <th>用户</th>
                                                        <th>币种</th>
                                                        <th>方向</th>
                                                        <th className="num-col">总盈亏</th>
                                                        <th className="num-col">持仓</th>
                                                        <th>状态</th>
                                                        <th>操作</th>
                                                    </tr>
                                                </thead>
                                                <tbody>
                                                    {entries.map(([pId, data]) => {
                                                        const p = data.pnl || {};
                                                        const owner = ownerMap[data.owner_id];
                                                        return (
                                                            <tr key={pId} className={data.health_status === 'reversal' ? 'row-reversal' : ''}>
                                                                <td>
                                                                    <span className="owner-chip">
                                                                        {owner?.display_name || owner?.username || data.owner_id?.slice(0, 8) || '?'}
                                                                    </span>
                                                                </td>
                                                                <td className="symbol-cell">{data.symbol || pId.split('_')[0]}</td>
                                                                <td>
                                                                    <span className="dir-tag long">{data.long_exchange}</span>
                                                                    <span className="dir-arrow">→</span>
                                                                    <span className="dir-tag short">{data.short_exchange}</span>
                                                                </td>
                                                                <td className={`num-col font-bold ${pnlClass(p.total_pnl)}`}>{fmtUsd(p.total_pnl)}</td>
                                                                <td className="num-col">{fmtDays(p.hours_held)}</td>
                                                                <td>{healthBadge(data.health_status)}</td>
                                                                <td><button className="delete-btn" onClick={() => handleDelete(pId)}>平仓</button></td>
                                                            </tr>
                                                        );
                                                    })}
                                                </tbody>
                                            </table>
                                        )}
                                    </div>
                                </div>
                            )}

                            {/* ══════════════════════════════════════════════
                                Tab: 用户管理 (admin only)
                            ══════════════════════════════════════════════ */}
                            {tab === 'users' && isAdmin && (
                                <div className="user-management">
                                    <h3>{editingUser ? `编辑用户: ${editingUser.username}` : '新增用户'}</h3>
                                    <form onSubmit={editingUser ? handleUpdateUser : handleCreateUser} className="user-form">
                                        <div className="user-form-row">
                                            <input type="text" placeholder="用户名" value={userForm.username} onChange={e => setUserForm({...userForm, username: e.target.value})} disabled={!!editingUser} required />
                                            <input type="password" placeholder={editingUser ? '新密码 (留空不变)' : '密码'} value={userForm.password} onChange={e => setUserForm({...userForm, password: e.target.value})} required={!editingUser} />
                                            <input type="text" placeholder="显示名称" value={userForm.display_name} onChange={e => setUserForm({...userForm, display_name: e.target.value})} />
                                            <select value={userForm.role} onChange={e => setUserForm({...userForm, role: e.target.value})}>
                                                <option value="trader">交易员</option>
                                                <option value="admin">管理员</option>
                                            </select>
                                        </div>
                                        <div className="user-form-row">
                                            <input type="text" placeholder="企业微信 Webhook (可选)" value={userForm.wecom_webhook} onChange={e => setUserForm({...userForm, wecom_webhook: e.target.value})} style={{ flex: 2 }} />
                                            <label style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
                                                <input type="checkbox" checked={userForm.enabled} onChange={e => setUserForm({...userForm, enabled: e.target.checked})} />
                                                启用
                                            </label>
                                            <button type="submit" disabled={loading}>{editingUser ? '保存' : '创建'}</button>
                                            {editingUser && <button type="button" className="logout-btn" onClick={() => { setEditingUser(null); setUserForm({ username: '', password: '', display_name: '', role: 'trader', wecom_webhook: '', enabled: true }); }}>取消</button>}
                                        </div>
                                    </form>

                                    <h3 style={{ marginTop: '24px' }}>用户列表</h3>
                                    <div className="user-table-wrap">
                                        <table className="pos-table">
                                            <thead>
                                                <tr>
                                                    <th>用户名</th>
                                                    <th>显示名</th>
                                                    <th>角色</th>
                                                    <th>Webhook</th>
                                                    <th>状态</th>
                                                    <th>操作</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {allUsers.map(u => (
                                                    <tr key={u.id}>
                                                        <td className="symbol-cell">{u.username}</td>
                                                        <td>{u.display_name || '-'}</td>
                                                        <td>
                                                            <span className={`role-badge ${u.role}`}>{u.role === 'admin' ? '管理员' : '交易员'}</span>
                                                        </td>
                                                        <td style={{ fontSize: '0.75rem', color: 'var(--text-muted)', maxWidth: '200px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                            {u.wecom_webhook || '-'}
                                                        </td>
                                                        <td>
                                                            <span className={`health-badge ${u.enabled ? 'healthy' : ''}`}>{u.enabled ? '启用' : '禁用'}</span>
                                                        </td>
                                                        <td>
                                                            <button className="delete-btn" style={{ marginRight: '4px' }} onClick={() => startEditUser(u)}>编辑</button>
                                                            {u.role !== 'admin' && (
                                                                <button className="delete-btn" onClick={() => handleDeleteUser(u.id, u.username)}>删除</button>
                                                            )}
                                                        </td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};

export default PositionsModal;
