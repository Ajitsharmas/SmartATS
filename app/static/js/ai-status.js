// ---------------------------------------------------------------------------
// AI status indicator — shared by every authenticated page's top nav.
// ---------------------------------------------------------------------------
//
// Each page's nav contains:
//   <button id="aiStatusBtn">
//     <span id="aiStatusDot"></span>
//     <span id="aiStatusLabel">Check AI</span>
//   </button>
//
// This module wires the click handler. Auto-binds on DOMContentLoaded.

(function () {
    function showAiStatusModal(type, message, provider) {
        const isUnavailable = type === 'unavailable';
        const iconColor   = isUnavailable ? 'text-amber-500' : 'text-red-600';
        const bgColor     = isUnavailable ? 'bg-amber-50'    : 'bg-red-50';
        const borderColor = isUnavailable ? 'border-amber-200': 'border-red-200';
        const title       = isUnavailable ? 'AI Temporarily Unavailable' : 'AI Provider Error';

        const existing = document.getElementById('_aiModal');
        if (existing) existing.remove();

        const modal = document.createElement('div');
        modal.id = '_aiModal';
        modal.innerHTML = `
            <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
                <div class="bg-white rounded-xl shadow-2xl max-w-sm w-full mx-4 p-6">
                    <div class="flex items-start gap-3 mb-4">
                        <div class="w-10 h-10 ${bgColor} border ${borderColor} rounded-full flex items-center justify-center flex-shrink-0">
                            <svg xmlns="http://www.w3.org/2000/svg" class="w-5 h-5 ${iconColor}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                        </div>
                        <div>
                            <h3 class="font-bold text-slate-800">${escapeHtml(title)}</h3>
                            <p class="text-sm text-slate-600 mt-1">${escapeHtml(message)}</p>
                        </div>
                    </div>
                    <p class="text-xs text-slate-400 mb-4">Provider: <span class="font-mono">${escapeHtml(provider)}</span></p>
                    <button id="_aiModalClose" class="w-full bg-indigo-600 text-white font-bold py-2 rounded-lg hover:bg-indigo-700 transition">OK</button>
                </div>
            </div>`;
        document.body.appendChild(modal);
        document.getElementById('_aiModalClose').addEventListener('click', () => modal.remove());
    }

    async function checkAi() {
        const btn   = document.getElementById('aiStatusBtn');
        const dot   = document.getElementById('aiStatusDot');
        const label = document.getElementById('aiStatusLabel');
        if (!btn || !dot || !label) return;

        btn.disabled = true;
        dot.className = 'w-2 h-2 rounded-full bg-slate-300 animate-pulse';
        label.textContent = 'Checking…';

        try {
            const res = await fetch('/health/ai');

            if (res.status === 429) {
                dot.className = 'w-2 h-2 rounded-full bg-slate-400';
                label.textContent = 'Check AI';
                showModal('You can only check the AI status twice per minute. Please wait a moment and try again.');
                return;
            }

            const data = await res.json();
            if (data.status === 'ok') {
                dot.className = 'w-2 h-2 rounded-full bg-green-500';
                label.textContent = 'AI Online';
                showModal('✓ ' + data.message, 'success');
            } else if (data.status === 'unavailable') {
                dot.className = 'w-2 h-2 rounded-full bg-amber-400';
                label.textContent = 'AI Unavailable';
                showAiStatusModal('unavailable', data.message, data.provider);
            } else {
                dot.className = 'w-2 h-2 rounded-full bg-red-500';
                label.textContent = 'AI Error';
                showAiStatusModal('error', data.message, data.provider);
            }
        } catch (_) {
            dot.className = 'w-2 h-2 rounded-full bg-red-500';
            label.textContent = 'AI Error';
            showAiStatusModal('error', 'Could not reach the server.', '—');
        } finally {
            btn.disabled = false;
        }
    }

    function highlightActiveTab() {
        // Pages set <body data-route="jobs|search|settings">.
        // We add the indigo accent class to the matching <a class="nav-link">.
        const route = document.body.dataset.route;
        if (!route) return;
        const link = document.querySelector(`.nav-link[data-route="${route}"]`);
        if (link) {
            link.classList.remove('text-slate-600');
            link.classList.add('text-indigo-600', 'font-semibold');
        }
    }

    function init() {
        const btn = document.getElementById('aiStatusBtn');
        if (btn) btn.addEventListener('click', checkAi);
        highlightActiveTab();
    }

    // Page <script>s sit at the bottom of <body>, so DOMContentLoaded has
    // typically already fired by the time we run. Handle both cases.
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
