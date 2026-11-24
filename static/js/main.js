/**
 * Career Catalyst — Main JavaScript
 * Handles global functionality: navbar scroll effect, smooth scrolling
 */

document.addEventListener('DOMContentLoaded', () => {

    // =============================================
    // Dark/Light Theme Toggle Switcher Initialization
    // =============================================
    const currentTheme = document.documentElement.getAttribute('data-theme') || localStorage.getItem('theme') || 'light';
    if (typeof window.applyAppTheme === 'function') {
        window.applyAppTheme(currentTheme);
    }

    // =============================================
    // Navbar Scroll Effect
    // =============================================
    const navbar = document.getElementById('main-navbar');
    
    if (navbar) {
        const handleScroll = () => {
            if (window.scrollY > 50) {
                navbar.classList.add('scrolled');
            } else {
                navbar.classList.remove('scrolled');
            }
        };

        window.addEventListener('scroll', handleScroll, { passive: true });
        // Run once on load in case page is already scrolled
        handleScroll();
    }

    // =============================================
    // Smooth Scrolling for Anchor Links
    // =============================================
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            const targetId = this.getAttribute('href');
            
            // Skip if it's just "#" or empty
            if (targetId === '#' || targetId === '') return;

            const targetElement = document.querySelector(targetId);
            if (targetElement) {
                e.preventDefault();
                
                const navbarHeight = navbar ? navbar.offsetHeight : 0;
                const targetPosition = targetElement.getBoundingClientRect().top + window.pageYOffset - navbarHeight - 20;

                window.scrollTo({
                    top: targetPosition,
                    behavior: 'smooth'
                });

                // Close mobile nav if open
                const navbarCollapse = document.querySelector('.navbar-collapse.show');
                if (navbarCollapse) {
                    const bsCollapse = bootstrap.Collapse.getInstance(navbarCollapse);
                    if (bsCollapse) {
                        bsCollapse.hide();
                    }
                }
            }
        });
    });

    // =============================================
    // Active Nav Link Highlighting on Scroll
    // =============================================
    const sections = document.querySelectorAll('section[id]');
    const navLinks = document.querySelectorAll('.navbar-nav .nav-link[href^="#"]');

    if (sections.length > 0 && navLinks.length > 0) {
        const highlightNav = () => {
            const scrollPos = window.scrollY + 200;

            sections.forEach(section => {
                const sectionTop = section.offsetTop;
                const sectionHeight = section.offsetHeight;
                const sectionId = section.getAttribute('id');

                if (scrollPos >= sectionTop && scrollPos < sectionTop + sectionHeight) {
                    navLinks.forEach(link => {
                        link.classList.remove('active');
                        if (link.getAttribute('href') === `#${sectionId}`) {
                            link.classList.add('active');
                        }
                    });
                }
            });
        };

        window.addEventListener('scroll', highlightNav, { passive: true });
    }

    // =============================================
    // Intersection Observer — Scroll Animations
    // =============================================
    const animatedElements = document.querySelectorAll('.animate-on-scroll');

    if (animatedElements.length > 0) {
        const observerOptions = {
            root: null,
            rootMargin: '0px 0px -80px 0px',
            threshold: 0.1
        };

        const animationObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('animated');
                    // Don't unobserve — animation only triggers once
                    animationObserver.unobserve(entry.target);
                }
            });
        }, observerOptions);

        animatedElements.forEach(el => {
            animationObserver.observe(el);
        });
    }

    // =============================================
    // Security / Anti-Inspect & Right-Click Protection
    // =============================================
    // Disable right-click context menu across the website
    document.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        return false;
    });

    // Disable keyboard shortcuts for Inspect Element, View Source, and DevTools
    document.addEventListener('keydown', (e) => {
        // F12 key
        if (e.key === 'F12' || e.keyCode === 123) {
            e.preventDefault();
            return false;
        }

        const isCtrlOrCmd = e.ctrlKey || e.metaKey;

        // Ctrl+Shift+I (DevTools), Ctrl+Shift+J (Console), Ctrl+Shift+C (Inspect Element)
        if (isCtrlOrCmd && e.shiftKey && (e.key === 'I' || e.key === 'i' || e.key === 'J' || e.key === 'j' || e.key === 'C' || e.key === 'c' || e.keyCode === 73 || e.keyCode === 74 || e.keyCode === 67)) {
            e.preventDefault();
            return false;
        }

        // Ctrl+U (View Source)
        if (isCtrlOrCmd && (e.key === 'u' || e.key === 'U' || e.keyCode === 85)) {
            e.preventDefault();
            return false;
        }

        // Ctrl+S (Save Webpage)
        if (isCtrlOrCmd && (e.key === 's' || e.key === 'S' || e.keyCode === 83)) {
            e.preventDefault();
            return false;
        }
    });

    // =============================================
    // Show / Hide Password Toggle on Password Fields
    // =============================================
    const passwordInputs = document.querySelectorAll('input[type="password"]');
    passwordInputs.forEach(input => {
        // Prevent duplicate wrappers
        if (input.closest('.password-toggle-wrapper')) return;

        // Create container wrapper
        const wrapper = document.createElement('div');
        wrapper.className = 'position-relative password-toggle-wrapper';

        // Insert wrapper into DOM and move input inside
        input.parentNode.insertBefore(wrapper, input);
        wrapper.appendChild(input);

        // Reserve space on the right for the eye icon
        input.style.paddingRight = '2.75rem';

        // Create toggle button
        const toggleBtn = document.createElement('button');
        toggleBtn.type = 'button';
        toggleBtn.className = 'btn btn-link position-absolute end-0 top-50 translate-middle-y text-muted text-decoration-none border-0 p-0 me-3 password-toggle-btn';
        toggleBtn.setAttribute('aria-label', 'Toggle password visibility');
        toggleBtn.setAttribute('tabindex', '-1');
        toggleBtn.innerHTML = '<i class="bi bi-eye-slash-fill fs-5"></i>';
        toggleBtn.style.zIndex = '5';
        toggleBtn.style.cursor = 'pointer';

        toggleBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (input.type === 'password') {
                input.type = 'text';
                toggleBtn.innerHTML = '<i class="bi bi-eye-fill fs-5 text-primary"></i>';
                toggleBtn.setAttribute('aria-label', 'Hide password');
            } else {
                input.type = 'password';
                toggleBtn.innerHTML = '<i class="bi bi-eye-slash-fill fs-5"></i>';
                toggleBtn.setAttribute('aria-label', 'Show password');
            }
        });

        wrapper.appendChild(toggleBtn);
    });

    // =============================================
    // Top Navigation Progress Bar
    // =============================================
    const progressBar = {
        element: null,
        timer: null,
        currentProgress: 0,
        init() {
            if (!this.element) {
                this.element = document.createElement('div');
                this.element.id = 'top-progress-bar';
                document.body.appendChild(this.element);
            }
        },
        start() {
            this.init();
            if (this.timer) clearInterval(this.timer);
            this.currentProgress = 15;
            this.element.style.opacity = '1';
            this.element.style.width = '15%';

            this.timer = setInterval(() => {
                if (this.currentProgress < 75) {
                    this.currentProgress += Math.random() * 15;
                    this.element.style.width = `${Math.min(this.currentProgress, 75)}%`;
                } else if (this.currentProgress < 90) {
                    this.currentProgress += Math.random() * 3;
                    this.element.style.width = `${Math.min(this.currentProgress, 90)}%`;
                }
            }, 200);
        },
        done() {
            if (!this.element) return;
            if (this.timer) clearInterval(this.timer);
            this.element.style.width = '100%';
            setTimeout(() => {
                this.element.style.opacity = '0';
                setTimeout(() => {
                    this.element.style.width = '0%';
                    this.currentProgress = 0;
                }, 300);
            }, 200);
        }
    };
    window.ProgressBar = progressBar;

    // Trigger progress bar on internal link clicks
    document.addEventListener('click', (e) => {
        const link = e.target.closest('a');
        if (!link) return;

        const href = link.getAttribute('href');
        const target = link.getAttribute('target');

        // Check if it's an external link, anchor hash, or new tab
        if (!href || href.startsWith('#') || href.startsWith('javascript:') || href.startsWith('mailto:') || href.startsWith('tel:') || target === '_blank') {
            return;
        }

        // Check if same origin
        try {
            const url = new URL(link.href, window.location.origin);
            if (url.origin === window.location.origin && url.pathname !== window.location.pathname) {
                progressBar.start();
            }
        } catch (err) {
            // Ignore URL parse error
        }
    });

    // Trigger on form submissions
    document.addEventListener('submit', (e) => {
        if (!e.defaultPrevented) {
            progressBar.start();
        }
    });

    // Complete on page load or pageshow (back/forward cache)
    window.addEventListener('pageshow', () => {
        progressBar.done();
    });

    // =============================================
    // Toast Notification System
    // =============================================
    const toastContainer = document.getElementById('toast-container');

    const initToast = (toastEl) => {
        if (!toastEl) return;
        const closeBtn = toastEl.querySelector('.toast-close-btn');
        const progressEl = toastEl.querySelector('.toast-progress');
        const duration = parseInt(toastEl.getAttribute('data-auto-dismiss') || '4000', 10);

        let timeoutId = null;
        let isPaused = false;
        let startTime = Date.now();
        let remainingTime = duration;

        const dismiss = () => {
            if (toastEl.classList.contains('toast-hiding')) return;
            toastEl.classList.add('toast-hiding');
            setTimeout(() => {
                toastEl.remove();
            }, 300);
        };

        if (progressEl) {
            progressEl.style.transition = `transform ${duration}ms linear`;
            // Trigger animation in next frame
            requestAnimationFrame(() => {
                progressEl.style.transform = 'scaleX(0)';
            });
        }

        const startTimer = (time) => {
            startTime = Date.now();
            timeoutId = setTimeout(dismiss, time);
        };

        const pauseTimer = () => {
            if (timeoutId) {
                clearTimeout(timeoutId);
                remainingTime -= (Date.now() - startTime);
                isPaused = true;
                if (progressEl) {
                    const computedWidth = window.getComputedStyle(progressEl).transform;
                    progressEl.style.transition = 'none';
                    progressEl.style.transform = computedWidth;
                }
            }
        };

        const resumeTimer = () => {
            if (isPaused && remainingTime > 0) {
                isPaused = false;
                if (progressEl) {
                    progressEl.style.transition = `transform ${remainingTime}ms linear`;
                    progressEl.style.transform = 'scaleX(0)';
                }
                startTimer(remainingTime);
            }
        };

        if (duration > 0) {
            startTimer(duration);
            toastEl.addEventListener('mouseenter', pauseTimer);
            toastEl.addEventListener('mouseleave', resumeTimer);
        }

        if (closeBtn) {
            closeBtn.addEventListener('click', () => {
                if (timeoutId) clearTimeout(timeoutId);
                dismiss();
            });
        }

        // Milestone Celebration Delighter: Automatically trigger celebratory confetti on key milestones
        const msgEl = toastEl.querySelector('.toast-msg');
        const msgText = (msgEl ? msgEl.textContent : '').toLowerCase();
        if ((toastEl.classList.contains('toast-success') || toastEl.className.includes('success')) && (
            msgText.includes('mentorship request sent') ||
            msgText.includes('session booked') ||
            msgText.includes('booking request sent') ||
            msgText.includes('certificate') ||
            msgText.includes('milestone completed') ||
            msgText.includes('track mastered')
        )) {
            setTimeout(() => {
                if (typeof window.triggerConfetti === 'function') {
                    window.triggerConfetti({ particleCount: 80, spread: 75, origin: { x: 0.5, y: 0.55 } });
                }
            }, 300);
        }
    };

    // Initialize all existing server toasts
    if (toastContainer) {
        toastContainer.querySelectorAll('.custom-toast').forEach(initToast);
    }

    // Global helper to show client-side toasts
    window.showToast = (message, type = 'info', duration = 4000) => {
        let container = document.getElementById('toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toast-container';
            container.className = 'toast-container-custom';
            container.setAttribute('aria-live', 'polite');
            container.setAttribute('aria-atomic', 'true');
            document.body.appendChild(container);
        }

        const icons = {
            success: 'bi-check-circle-fill text-success',
            error: 'bi-exclamation-octagon-fill text-danger',
            danger: 'bi-exclamation-octagon-fill text-danger',
            warning: 'bi-exclamation-triangle-fill text-warning',
            info: 'bi-info-circle-fill text-primary'
        };

        const toastEl = document.createElement('div');
        toastEl.className = `custom-toast toast-${type}`;
        toastEl.setAttribute('role', 'alert');
        toastEl.setAttribute('data-auto-dismiss', duration);

        toastEl.innerHTML = `
            <div class="toast-icon">
                <i class="bi ${icons[type] || icons.info}"></i>
            </div>
            <div class="toast-body-content">
                <div class="toast-msg">${message}</div>
            </div>
            <button type="button" class="toast-close-btn" aria-label="Close">
                <i class="bi bi-x"></i>
            </button>
            <div class="toast-progress"></div>
        `;

        container.appendChild(toastEl);
        initToast(toastEl);
    };

    // Global password visibility toggle helper
    window.togglePasswordVisibility = function(inputId, btn) {
        const input = document.getElementById(inputId);
        if (!input) return;
        const icon = btn.querySelector('i');
        if (input.type === 'password') {
            input.type = 'text';
            if (icon) {
                icon.classList.remove('bi-eye');
                icon.classList.add('bi-eye-slash');
            }
        } else {
            input.type = 'password';
            if (icon) {
                icon.classList.remove('bi-eye-slash');
                icon.classList.add('bi-eye');
            }
        }
    };

    // Global interactive password strength and match checker
    window.initPasswordStrengthChecker = function(passwordInputId, options) {
        options = options || {};
        const passwordInput = document.getElementById(passwordInputId);
        if (!passwordInput) return;

        const wrapper = document.getElementById(options.wrapperId || 'passwordStrengthWrapper');
        const label = document.getElementById(options.labelId || 'passwordStrengthLabel');
        const critLength = document.getElementById(options.critLengthId || 'crit-length');
        const critMixed = document.getElementById(options.critMixedId || 'crit-mixed');
        const critSpecial = document.getElementById(options.critSpecialId || 'crit-special');
        const confirmInput = options.confirmInputId ? document.getElementById(options.confirmInputId) : null;
        const matchFeedback = options.matchFeedbackId ? document.getElementById(options.matchFeedbackId) : null;

        function evaluateStrength(val) {
            if (!val || val.length === 0) return 0;
            let score = 0;
            
            const hasLength = val.length >= 8;
            const hasLower = /[a-z]/.test(val);
            const hasUpper = /[A-Z]/.test(val);
            const hasNumber = /[0-9]/.test(val);
            const hasSpecial = /[^A-Za-z0-9]/.test(val);
            const hasMixed = (hasLower || hasUpper) && hasNumber;

            // Update criterion chips
            if (critLength) {
                critLength.classList.toggle('satisfied', hasLength);
                const icon = critLength.querySelector('i');
                if (icon) icon.className = hasLength ? 'bi bi-check-circle-fill text-success' : 'bi bi-circle';
            }
            if (critMixed) {
                critMixed.classList.toggle('satisfied', hasMixed);
                const icon = critMixed.querySelector('i');
                if (icon) icon.className = hasMixed ? 'bi bi-check-circle-fill text-success' : 'bi bi-circle';
            }
            if (critSpecial) {
                critSpecial.classList.toggle('satisfied', hasSpecial);
                const icon = critSpecial.querySelector('i');
                if (icon) icon.className = hasSpecial ? 'bi bi-check-circle-fill text-success' : 'bi bi-circle';
            }

            if (val.length < 6) return 1;
            
            let varietyCount = 0;
            if (hasLower) varietyCount++;
            if (hasUpper) varietyCount++;
            if (hasNumber) varietyCount++;
            if (hasSpecial) varietyCount++;

            if (val.length >= 8) score++;
            if (varietyCount >= 2) score++;
            if (varietyCount >= 3 && val.length >= 8) score++;
            if (varietyCount >= 4 || (varietyCount >= 3 && val.length >= 12)) score++;

            return Math.min(Math.max(score, 1), 4);
        }

        const labels = {
            0: 'Enter password',
            1: 'Weak',
            2: 'Fair',
            3: 'Good',
            4: 'Strong ✓'
        };

        function update() {
            const val = passwordInput.value;
            if (!val || val.length === 0) {
                if (wrapper) wrapper.style.display = 'none';
                if (matchFeedback) matchFeedback.style.display = 'none';
                return;
            }

            if (wrapper) wrapper.style.display = 'block';
            const score = evaluateStrength(val);
            if (wrapper) wrapper.setAttribute('data-strength', score);
            if (label) label.textContent = labels[score];

            checkMatch();
        }

        function checkMatch() {
            if (!confirmInput || !matchFeedback) return;
            const passVal = passwordInput.value;
            const confirmVal = confirmInput.value;

            if (!confirmVal || confirmVal.length === 0) {
                matchFeedback.style.display = 'none';
                return;
            }

            matchFeedback.style.display = 'flex';
            if (passVal === confirmVal) {
                matchFeedback.innerHTML = '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i> Passwords match</span>';
            } else {
                matchFeedback.innerHTML = '<span class="text-warning"><i class="bi bi-dash-circle me-1"></i> Passwords don\'t match yet</span>';
            }
        }

        passwordInput.addEventListener('input', update);
        passwordInput.addEventListener('focus', function() {
            if (passwordInput.value.length > 0 && wrapper) wrapper.style.display = 'block';
        });

        if (confirmInput) {
            confirmInput.addEventListener('input', checkMatch);
        }
    };

    // Global Skeleton Wireframe Generator for Dynamic Views
    window.createSkeleton = function(type, count) {
        count = count || 1;
        let html = '';
        for (let i = 0; i < count; i++) {
            if (type === 'card') {
                html += `
                    <div class="skeleton-card mb-3" aria-hidden="true">
                        <div class="d-flex align-items-center gap-3">
                            <div class="skeleton skeleton-avatar"></div>
                            <div class="flex-grow-1">
                                <div class="skeleton skeleton-title" style="width: 50%;"></div>
                                <div class="skeleton skeleton-text short"></div>
                            </div>
                        </div>
                        <div class="skeleton skeleton-text mt-2"></div>
                        <div class="skeleton skeleton-text medium"></div>
                    </div>
                `;
            } else if (type === 'row') {
                html += `
                    <div class="skeleton-row" aria-hidden="true">
                        <div class="skeleton skeleton-avatar-sm"></div>
                        <div class="flex-grow-1">
                            <div class="skeleton skeleton-text medium" style="margin-bottom: 0.25rem;"></div>
                            <div class="skeleton skeleton-text short" style="margin-bottom: 0;"></div>
                        </div>
                        <div class="skeleton skeleton-pill" style="width: 70px; height: 24px;"></div>
                    </div>
                `;
            } else if (type === 'slots') {
                html += `
                    <div class="skeleton-slot-loader py-1" aria-hidden="true">
                        <div class="skeleton skeleton-pill me-2 mb-2" style="width: 135px; height: 32px;"></div>
                        <div class="skeleton skeleton-pill me-2 mb-2" style="width: 135px; height: 32px;"></div>
                        <div class="skeleton skeleton-pill me-2 mb-2" style="width: 135px; height: 32px;"></div>
                        <div class="skeleton skeleton-pill me-2 mb-2" style="width: 135px; height: 32px;"></div>
                    </div>
                `;
            }
        }
        return html;
    };

    // Tactile Perceived Navigation Progress Indicator
    let topProgressBar = null;
    function getTopProgressBar() {
        if (!topProgressBar) {
            topProgressBar = document.createElement('div');
            topProgressBar.className = 'cc-top-progress';
            topProgressBar.setAttribute('aria-hidden', 'true');
            document.body.appendChild(topProgressBar);
        }
        return topProgressBar;
    }

    window.startPageProgress = function() {
        const bar = getTopProgressBar();
        bar.classList.remove('done');
        bar.classList.add('active');
    };

    window.finishPageProgress = function() {
        if (!topProgressBar) return;
        topProgressBar.classList.add('done');
        setTimeout(() => {
            topProgressBar.classList.remove('active', 'done');
        }, 400);
    };

    // Attach instant progress feedback to form submits across the platform
    document.addEventListener('submit', function(e) {
        const form = e.target;
        if (form && !form.hasAttribute('data-no-progress') && form.checkValidity && form.checkValidity()) {
            window.startPageProgress();
        }
    }, { passive: true });

    // =========================================================================
    // Dynamic Rolling Number Odometers & Fluid Count-Up System
    // Deceleration Easing: cubic-bezier(0.16, 1, 0.3, 1) — Apple / Linear curve
    // =========================================================================
    function solveCubicBezier(x1, y1, x2, y2) {
        return function(t) {
            if (t <= 0) return 0;
            if (t >= 1) return 1;
            let s = t;
            for (let i = 0; i < 6; i++) {
                const currentX = 3 * (1 - s) * (1 - s) * s * x1 + 3 * (1 - s) * s * s * x2 + s * s * s;
                const dx = 3 * (1 - s) * (1 - s) * x1 + 6 * (1 - s) * s * (x2 - x1) + 3 * s * s * (1 - x2);
                if (Math.abs(currentX - t) < 1e-5) break;
                if (Math.abs(dx) < 1e-6) break;
                s -= (currentX - t) / dx;
            }
            s = Math.max(0, Math.min(1, s));
            return 3 * (1 - s) * (1 - s) * s * y1 + 3 * (1 - s) * s * s * y2 + s * s * s;
        };
    }

    const easeFluidDecelerate = solveCubicBezier(0.16, 1, 0.3, 1);

    window.animateCountUp = function(el, endVal, options) {
        if (!el) return;
        if (typeof el === 'string') el = document.querySelector(el);
        if (!el) return;

        options = options || {};
        const duration = options.duration !== undefined ? options.duration : 1200;
        const prefix = options.prefix || el.getAttribute('data-prefix') || '';
        const suffix = options.suffix || el.getAttribute('data-suffix') || '';

        // Extract raw number value if not passed
        if (endVal === undefined || endVal === null) {
            const rawAttr = el.getAttribute('data-count-up') || el.getAttribute('data-target');
            if (rawAttr !== null) {
                endVal = parseFloat(rawAttr);
            } else {
                const textOnly = el.textContent.replace(/[^\d.-]/g, '');
                endVal = textOnly ? parseFloat(textOnly) : 0;
            }
        }
        if (isNaN(endVal)) return;

        const decimals = options.decimals !== undefined ? options.decimals : (String(endVal).includes('.') ? String(endVal).split('.')[1].length : 0);

        // Parse start value
        let startVal = options.startVal !== undefined ? options.startVal : parseFloat(el.getAttribute('data-start') || 0);
        if (isNaN(startVal)) startVal = 0;

        // Accessibility check: instantly snap to target if user prefers reduced motion
        const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (prefersReducedMotion || duration <= 0) {
            const formatted = decimals > 0 ? endVal.toFixed(decimals) : Math.round(endVal);
            el.textContent = `${prefix}${formatted}${suffix}`;
            if (typeof options.onComplete === 'function') options.onComplete();
            return;
        }

        if (el._ccCountUpActive) {
            cancelAnimationFrame(el._ccCountUpActive);
        }

        const startTime = performance.now();
        const diff = endVal - startVal;

        function frame(now) {
            const elapsed = now - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const easedProgress = easeFluidDecelerate(progress);
            const currentVal = startVal + diff * easedProgress;

            const formatted = decimals > 0 ? currentVal.toFixed(decimals) : Math.round(currentVal);
            el.textContent = `${prefix}${formatted}${suffix}`;

            if (typeof options.onUpdate === 'function') {
                options.onUpdate(currentVal, easedProgress);
            }

            if (progress < 1) {
                el._ccCountUpActive = requestAnimationFrame(frame);
            } else {
                el._ccCountUpActive = null;
                const finalFormatted = decimals > 0 ? endVal.toFixed(decimals) : Math.round(endVal);
                el.textContent = `${prefix}${finalFormatted}${suffix}`;
                if (typeof options.onComplete === 'function') {
                    options.onComplete();
                }
            }
        }

        el._ccCountUpActive = requestAnimationFrame(frame);
    };

    // Auto-initialize [data-count-up] elements using IntersectionObserver
    window.initCountUpElements = function() {
        const targets = document.querySelectorAll('[data-count-up]:not([data-counted])');
        if (!targets.length) return;

        if (!('IntersectionObserver' in window)) {
            targets.forEach(el => {
                el.setAttribute('data-counted', 'true');
                window.animateCountUp(el);
            });
            return;
        }

        const observer = new IntersectionObserver((entries, obs) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const el = entry.target;
                    obs.unobserve(el);
                    el.setAttribute('data-counted', 'true');
                    const targetVal = parseFloat(el.getAttribute('data-count-up'));
                    window.animateCountUp(el, targetVal);
                }
            });
        }, { threshold: 0.15 });

        targets.forEach(el => observer.observe(el));
    };

    window.initCountUpElements();

    // =========================================================================
    // Native Zero-Dependency Canvas Confetti Engine
    // Physics: 3D Tumbling Rotation, Air Resistance Drag, Gravity & Alpha Decay
    // =========================================================================
    (function() {
        let canvas = null;
        let ctx = null;
        let particles = [];
        let animationFrameId = null;
        let width = 0;
        let height = 0;

        const PALETTE = [
            '#2563EB', // Primary Royal Blue
            '#3B82F6', // Electric Blue
            '#10B981', // Emerald Success
            '#06B6D4', // Modern Cyan
            '#F59E0B', // Amber Milestone
            '#8B5CF6', // Purple Highlight
            '#EC4899'  // Pink Accent
        ];

        function getCanvas() {
            if (!canvas) {
                canvas = document.createElement('canvas');
                canvas.id = 'cc-confetti-canvas';
                canvas.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;pointer-events:none;z-index:999999;display:none;';
                document.body.appendChild(canvas);
                ctx = canvas.getContext('2d');
                resize();
                window.addEventListener('resize', resize, { passive: true });
            }
            return canvas;
        }

        function resize() {
            if (!canvas) return;
            const dpr = window.devicePixelRatio || 1;
            width = window.innerWidth;
            height = window.innerHeight;
            canvas.width = width * dpr;
            canvas.height = height * dpr;
            if (ctx) ctx.scale(dpr, dpr);
        }

        function createParticle(origin, options) {
            const spread = (options.spread || 70) * (Math.PI / 180);
            const angle = ((options.angle !== undefined ? options.angle : 90)) * (Math.PI / 180);
            const velocity = (options.startVelocity || 38) * (0.8 + Math.random() * 0.4);
            const randomAngle = angle + (Math.random() - 0.5) * spread;

            const colors = options.colors || PALETTE;
            const color = colors[Math.floor(Math.random() * colors.length)];
            const shape = Math.random() > 0.45 ? 'rect' : 'circle';
            const size = (options.scalar || 1) * (6.5 + Math.random() * 4);

            return {
                x: origin.x * width,
                y: origin.y * height,
                vx: Math.cos(randomAngle) * velocity,
                vy: -Math.sin(randomAngle) * velocity,
                gravity: options.gravity !== undefined ? options.gravity : 0.68,
                decay: options.decay || 0.925,
                tilt: Math.random() * 10,
                tiltAngle: Math.random() * Math.PI,
                tiltAngleIncrement: 0.06 + Math.random() * 0.08,
                shape: shape,
                size: size,
                color: color,
                opacity: 1,
                life: 0,
                maxLife: (options.ticks || 160) + Math.random() * 50
            };
        }

        function updateParticles() {
            for (let i = particles.length - 1; i >= 0; i--) {
                const p = particles[i];
                p.x += p.vx;
                p.y += p.vy;
                p.vy += p.gravity;
                p.vx *= p.decay;
                p.vy *= p.decay;
                p.tiltAngle += p.tiltAngleIncrement;
                p.tilt = Math.sin(p.tiltAngle) * 12;
                p.life++;

                if (p.life > p.maxLife * 0.72) {
                    p.opacity = Math.max(0, 1 - (p.life - p.maxLife * 0.72) / (p.maxLife * 0.28));
                }

                if (p.opacity <= 0 || p.y > height + 40) {
                    particles.splice(i, 1);
                }
            }
        }

        function drawParticles() {
            if (!ctx) return;
            ctx.clearRect(0, 0, width, height);

            for (let i = 0; i < particles.length; i++) {
                const p = particles[i];
                ctx.save();
                ctx.globalAlpha = p.opacity;
                ctx.fillStyle = p.color;
                ctx.translate(p.x, p.y);
                ctx.rotate(p.tiltAngle);

                if (p.shape === 'circle') {
                    ctx.beginPath();
                    ctx.ellipse(0, 0, p.size / 2, Math.max(0.5, Math.abs(Math.sin(p.tiltAngle)) * (p.size / 2)), 0, 0, Math.PI * 2);
                    ctx.fill();
                } else {
                    const w = p.size;
                    const h = p.size * 0.55;
                    ctx.scale(Math.cos(p.tiltAngle), 1);
                    ctx.fillRect(-w / 2, -h / 2, w, h);
                }
                ctx.restore();
            }
        }

        function loop() {
            updateParticles();
            drawParticles();

            if (particles.length > 0) {
                animationFrameId = requestAnimationFrame(loop);
            } else {
                animationFrameId = null;
                if (canvas) canvas.style.display = 'none';
            }
        }

        window.triggerConfetti = function(options) {
            options = options || {};
            if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
                return;
            }

            const cvs = getCanvas();
            cvs.style.display = 'block';

            const origin = options.origin || { x: 0.5, y: 0.6 };
            const count = options.particleCount || 75;

            for (let i = 0; i < count; i++) {
                particles.push(createParticle(origin, options));
            }

            if (!animationFrameId) {
                animationFrameId = requestAnimationFrame(loop);
            }
        };

        // Milestone Cannons Preset: Dual corner cannons for major achievements
        window.triggerConfetti.cannons = function() {
            if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
            window.triggerConfetti({
                particleCount: 50,
                angle: 60,
                spread: 55,
                origin: { x: 0.05, y: 0.85 }
            });
            setTimeout(() => {
                window.triggerConfetti({
                    particleCount: 50,
                    angle: 120,
                    spread: 55,
                    origin: { x: 0.95, y: 0.85 }
                });
            }, 160);
        };

        // Fallback alias so anywhere calling confetti(...) in the platform works seamlessly
        if (typeof window.confetti !== 'function') {
            window.confetti = window.triggerConfetti;
        }
    })();

    // =========================================================================
    // Cursor-Following Ambient Border Beam Pointer Tracker (The Vercel / Stripe Aesthetic)
    // 60/120fps hardware-accelerated tracking, zero layout reflows, passive listener
    // =========================================================================
    (function initAmbientBorderBeams() {
        // Only run on fine-pointer devices (desktop mice / trackpads)
        if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

        let activeCard = null;
        let cachedRect = null;
        let pendingRaf = null;
        let mouseX = 0;
        let mouseY = 0;

        const SELECTOR = '.ambient-beam-card, .dash-next-step-card, .mentor-card, .roadmap-catalog-card, .roadmap-detail-header-card, .dash-panel-card, .ats-report-hero, [data-ambient-beam]';

        function updateCoords(e) {
            if (!activeCard || !cachedRect) return;
            mouseX = e.clientX - cachedRect.left;
            mouseY = e.clientY - cachedRect.top;

            if (!pendingRaf) {
                pendingRaf = requestAnimationFrame(() => {
                    if (activeCard) {
                        activeCard.style.setProperty('--mouse-x', `${mouseX}px`);
                        activeCard.style.setProperty('--mouse-y', `${mouseY}px`);
                    }
                    pendingRaf = null;
                });
            }
        }

        document.addEventListener('pointerover', (e) => {
            const card = e.target.closest(SELECTOR);
            if (!card) return;
            if (activeCard !== card) {
                activeCard = card;
                cachedRect = card.getBoundingClientRect();
                updateCoords(e);
            }
        }, { passive: true });

        document.addEventListener('pointermove', updateCoords, { passive: true });

        document.addEventListener('pointerout', (e) => {
            if (activeCard && (!e.relatedTarget || !activeCard.contains(e.relatedTarget))) {
                activeCard = null;
                cachedRect = null;
                if (pendingRaf) {
                    cancelAnimationFrame(pendingRaf);
                    pendingRaf = null;
                }
            }
        }, { passive: true });

        window.addEventListener('scroll', () => {
            if (activeCard) {
                cachedRect = activeCard.getBoundingClientRect();
            }
        }, { passive: true });
    })();

    // =========================================================================
    // Native Browser View Transitions & Morph System
    // Enables seamless card-to-detail morphs and zero-flash transitions
    // =========================================================================
    window.withViewTransition = function(fn) {
        if (document.startViewTransition && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            return document.startViewTransition(fn);
        }
        return fn();
    };

    // Dynamically tag clicked cards for seamless cross-page morphing
    document.addEventListener('click', (e) => {
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

        // Mentor Card -> Mentor Detail Header Morph
        const mentorTrigger = e.target.closest('.mentor-card a, a[href*="/community/mentor/"]');
        if (mentorTrigger && !e.ctrlKey && !e.metaKey && !e.shiftKey && mentorTrigger.target !== '_blank') {
            const card = mentorTrigger.closest('.mentor-card');
            if (card) {
                document.querySelectorAll('.mentor-card-active-morph').forEach(el => el.classList.remove('mentor-card-active-morph'));
                card.classList.add('mentor-card-active-morph');
            }
        }

        // Roadmap Card -> Roadmap Detail Header Morph
        const roadmapTrigger = e.target.closest('.roadmap-catalog-card a, a[href*="/roadmaps/learning/"]');
        if (roadmapTrigger && !e.ctrlKey && !e.metaKey && !e.shiftKey && roadmapTrigger.target !== '_blank') {
            const card = roadmapTrigger.closest('.roadmap-catalog-card');
            if (card) {
                document.querySelectorAll('.roadmap-card-active-morph').forEach(el => el.classList.remove('roadmap-card-active-morph'));
                card.classList.add('roadmap-card-active-morph');
            }
        }
    }, { passive: true });

});
