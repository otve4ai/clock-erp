(() => {
    // `attention` includes product remarks, even for imported/sold orders.
    // `missing` and `supplies` describe the state BEFORE recovery imported them.
    window.wbSyncStatus = data => {
        const date = data.last_success_at ? new Date(typeof data.last_success_at === 'number' ? data.last_success_at * 1000 : data.last_success_at) : null;
        const valid = date && !Number.isNaN(date.getTime());
        const stale = !valid || Date.now() - date.getTime() > 15 * 60000;
        const issues = Boolean(data.pending?.length || data.errors?.length || data.full_errors?.length || data.error_count > 0 || data.outcome === 'partial' || ['partial', 'error'].includes(data.full_outcome));
        const state = data.outcome === 'running' ? 'running' : data.outcome === 'error' ? 'error' : stale || issues ? 'attention' : 'success';
        return {state, noData: !valid && !issues};
    };
})();
