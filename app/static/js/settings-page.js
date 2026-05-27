// ---------------------------------------------------------------------------
// /settings — Account-level controls. Change password, log out, show email.
// ---------------------------------------------------------------------------

(function () {
    if (!localStorage.getItem('access_token')) {
        window.location.href = '/login';
        return;
    }

    // Pull the email out of the JWT payload — same pattern as the old header.
    let email = '';
    const token = Api.getToken();
    if (token) {
        try {
            email = JSON.parse(atob(token.split('.')[1])).sub || '';
        } catch (_) { /* malformed token, leave email blank */ }
    }
    document.getElementById('settingsEmail').textContent = email || 'unknown';

    document.getElementById('changePasswordBtn').addEventListener('click', () => {
        showConfirmModal({
            title: 'Change Password',
            details: [{ label: 'Email', value: email || 'your account email' }],
            confirmText: 'Send Reset Link',
            note: 'A password reset link will be sent to your email address.',
            onConfirm: async () => {
                if (!email) {
                    showModal('Could not determine your email. Please log out and use Forgot Password on the login page.');
                    return;
                }
                try {
                    const res = await fetch('/forgot-password', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ email }),
                    });
                    const data = await res.json();
                    showModal(data.message || 'Reset link sent. Please check your inbox.', 'success');
                } catch (_) {
                    showModal('Something went wrong. Please try again.');
                }
            },
        });
    });

    document.getElementById('logoutBtn').addEventListener('click', () => {
        Api.logout();
    });
})();
