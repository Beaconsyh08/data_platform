// Account state and behavior for the legacy console.
function consoleAccountState() {
    return {
                controlPlaneUser: {{ control_plane_user|tojson }},
                adminModeEnabled: {{ admin_authenticated|tojson }},
                adminPasswordConfigured: false,
                adminMenuOpen: false,
                adminDialogOpen: false,
                adminDialogMode: 'login',
                adminDialogBusy: false,
                adminDialogError: '',
                adminForm: {currentPassword: '', password: '', confirmPassword: ''},
                async loadAdminStatus() {
                    if (this.controlPlaneEnabled) {
                        this.adminPasswordConfigured = true;
                        this.adminModeEnabled = Boolean(
                            this.controlPlaneUser && this.controlPlaneUser.role === 'admin'
                        );
                        return;
                    }
                    const wasEnabled = this.adminModeEnabled;
                    const data = await this.requestJson('/api/admin/status', null, 'GET');
                    this.adminPasswordConfigured = Boolean(data.configured);
                    this.adminModeEnabled = Boolean(data.authenticated);
                    if (wasEnabled && !this.adminModeEnabled) {
                        this.adminMenuOpen = false;
                        this.clearAdminOperationState();
                        if (this.activeWorkspace === 'legacy_admin') {
                            await this.selectWorkspace('data_platform');
                        }
                    }
                },
                accountButtonLabel() {
                    if (!this.controlPlaneEnabled) {
                        return this.adminModeEnabled ? 'Admin active' : 'Enter Admin';
                    }
                    const name = this.controlPlaneUser?.username || 'Account';
                    return `${name} · ${this.adminModeEnabled ? 'Admin' : (this.controlPlaneUser?.role || 'User')}`;
                },
                adminDialogTitle() {
                    if (this.adminDialogMode === 'setup') return 'Set administrator password';
                    if (this.adminDialogMode === 'change') return 'Change administrator password';
                    return 'Enter Admin Mode';
                },
                adminDialogDescription() {
                    if (this.adminDialogMode === 'setup') {
                        return 'Create the local password for this dataset root. Only a salted hash is stored.';
                    }
                    if (this.adminDialogMode === 'change') {
                        return 'Changing the password revokes other active administrator sessions.';
                    }
                    return 'Unlock in-place deletion and legacy mutation controls until you explicitly exit.';
                },
                adminDialogSubmitLabel() {
                    if (this.adminDialogMode === 'setup') return 'Set password & enter';
                    if (this.adminDialogMode === 'change') return 'Change password';
                    return 'Enter Admin Mode';
                },
                openAdminDialog(mode = '') {
                    this.adminDialogMode = mode || (this.adminPasswordConfigured ? 'login' : 'setup');
                    this.adminDialogError = '';
                    this.adminForm = {currentPassword: '', password: '', confirmPassword: ''};
                    this.adminDialogOpen = true;
                },
                closeAdminDialog() {
                    if (this.adminDialogBusy) return;
                    this.adminDialogOpen = false;
                    this.adminDialogError = '';
                    this.adminForm = {currentPassword: '', password: '', confirmPassword: ''};
                },
                async submitAdminDialog() {
                    if (this.adminDialogBusy) return;
                    this.adminDialogError = '';
                    this.adminDialogBusy = true;
                    try {
                        let endpoint = '/api/admin/login';
                        let payload = {password: this.adminForm.password};
                        if (this.adminDialogMode === 'setup') {
                            endpoint = '/api/admin/setup';
                            payload.confirm_password = this.adminForm.confirmPassword;
                        } else if (this.adminDialogMode === 'change') {
                            endpoint = '/api/admin/password';
                            payload = {
                                current_password: this.adminForm.currentPassword,
                                new_password: this.adminForm.password,
                                confirm_password: this.adminForm.confirmPassword
                            };
                        }
                        const data = await this.requestJson(endpoint, payload);
                        this.adminPasswordConfigured = Boolean(data.configured);
                        this.adminModeEnabled = Boolean(data.authenticated);
                        this.adminMenuOpen = false;
                        this.adminDialogOpen = false;
                        this.adminForm = {currentPassword: '', password: '', confirmPassword: ''};
                        if (this.activeTab === 'quality_flags') {
                            await this.loadDeleteFlagReasonOptions(true);
                        }
                    } catch (err) {
                        this.adminDialogError = err.message;
                    } finally {
                        this.adminDialogBusy = false;
                    }
                },
                clearAdminOperationState() {
                    for (const field of this.mutationFields) this.options[field.key] = false;
                    this.preprocess.delete_flag_reason = '';
                    this.preprocess.delete_episode_ids = '';
                    this.preprocess.delete_reason = '';
                    this.preprocess.flag_delete_reason = '';
                },
                async disableAdminMode() {
                    if (this.controlPlaneEnabled) {
                        await this.signOutAccount();
                        return;
                    }
                    try {
                        await this.requestJson('/api/admin/logout', {}, 'POST');
                    } catch (err) {
                        this.error = err.message;
                        return;
                    }
                    this.adminModeEnabled = false;
                    this.adminMenuOpen = false;
                    this.clearAdminOperationState();
                    if (this.activeWorkspace === 'legacy_admin') {
                        await this.selectWorkspace('data_platform');
                    }
                },
                async signOutAccount() {
                    try {
                        await this.requestJson('/api/auth/logout', {}, 'POST');
                        window.location.assign('/login');
                    } catch (err) {
                        this.error = err.message;
                    }
                },
                async toggleAdminMode() {
                    if (this.controlPlaneEnabled) {
                        this.adminMenuOpen = !this.adminMenuOpen;
                        return;
                    }
                    if (this.adminModeEnabled) {
                        this.adminMenuOpen = !this.adminMenuOpen;
                        return;
                    }
                    this.openAdminDialog();
                },
    };
}
