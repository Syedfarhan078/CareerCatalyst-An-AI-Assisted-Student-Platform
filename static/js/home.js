/**
 * CareerCatalyst — Home Page JavaScript
 * Clean, lightweight, accessible.
 * Respects prefers-reduced-motion.
 */

document.addEventListener('DOMContentLoaded', () => {
    // 1. Reveal animations with IntersectionObserver
    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!prefersReducedMotion) {
        const revealElements = document.querySelectorAll('.animate-on-scroll');
        if (revealElements.length > 0 && 'IntersectionObserver' in window) {
            const revealObserver = new IntersectionObserver((entries) => {
                entries.forEach(entry => {
                    if (entry.isIntersecting) {
                        entry.target.classList.add('animated');
                        revealObserver.unobserve(entry.target);
                    }
                });
            }, {
                root: null,
                rootMargin: '0px 0px -40px 0px',
                threshold: 0.1
            });

            revealElements.forEach(el => revealObserver.observe(el));
        }
    }

    // 2. Try-Before-Signup Interactive ATS Quick-Scanner
    initQuickScan();
});

function initQuickScan() {
    const textarea = document.getElementById('quickscan-text');
    const roleSelect = document.getElementById('quickscan-role');
    const submitBtn = document.getElementById('quickscan-submit-btn');
    const btnSpinner = document.getElementById('quickscan-btn-spinner');
    const btnIcon = document.getElementById('quickscan-btn-icon');
    const btnText = document.getElementById('quickscan-btn-text');
    const charCounter = document.getElementById('quickscan-char-count');
    const errorBox = document.getElementById('quickscan-error-msg');
    const errorText = document.getElementById('quickscan-error-text');

    const stateIdle = document.getElementById('quickscan-state-idle');
    const stateActive = document.getElementById('quickscan-state-active');

    if (!textarea || !submitBtn) return;

    const samplePresets = {
        swe: {
            role: 'Software Engineer',
            text: `John Doe\njohn.doe@example.com | +1 555-0199 | github.com/johndoe\n\nPROFESSIONAL SUMMARY\nSoftware Engineer with experience developing scalable web applications and REST APIs using Python, Django, and PostgreSQL.\n\nEXPERIENCE\nSoftware Engineer | CloudScale Inc.\n- Architected high-throughput REST APIs using Python, Django, and PostgreSQL serving 120,000 active users.\n- Optimized complex SQL queries and implemented Redis caching, reducing p95 database response latency by 45%.\n- Implemented Git and Docker containerization within automated CI/CD deployment pipelines.\n- Automated test coverage using PyTest, achieving 88% unit and integration test coverage.\n\nSKILLS\nPython, Java, JavaScript, Git, SQL, PostgreSQL, REST API, System Design, Data Structures, Algorithms`
        },
        qa: {
            role: 'QA Manual Tester',
            text: `Jane Smith\njane.smith@example.com | +1 555-0188 | linkedin.com/in/janesmith\n\nEXPERIENCE\nQA Tester | Apex Digital\n- Authored 300+ comprehensive test cases and test scenarios for e-commerce web applications.\n- Executed regular regression testing, smoke testing, and functional testing across agile sprints.\n- Logged and tracked 150+ software defects in Jira throughout the full defect lifecycle.\n- Validated REST API endpoints using Postman, verifying response schemas and status codes.\n\nTECHNICAL SKILLS\nManual Testing, Test Cases, Jira, Regression Testing, Bug Tracking, API Testing, Postman, SDLC, STLC, SQL`
        }
    };

    // Preset buttons
    document.querySelectorAll('.quickscan-preset-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const presetKey = btn.dataset.preset;
            const preset = samplePresets[presetKey];
            if (preset) {
                roleSelect.value = preset.role;
                textarea.value = preset.text;
                updateCharCount();
                hideError();
                // Smoothly focus textarea
                textarea.focus();
            }
        });
    });

    // Character counter
    textarea.addEventListener('input', updateCharCount);

    function updateCharCount() {
        const count = textarea.value.length;
        if (charCounter) charCounter.textContent = count;
    }

    function showError(msg) {
        if (errorBox && errorText) {
            errorText.textContent = msg;
            errorBox.classList.remove('d-none');
        }
    }

    function hideError() {
        if (errorBox) errorBox.classList.add('d-none');
    }

    // Submit handler
    submitBtn.addEventListener('click', async () => {
        const text = textarea.value.trim();
        const role = roleSelect.value;

        hideError();

        if (text.length < 30) {
            showError('Please paste at least 30 characters of resume content or experience bullets to analyze.');
            textarea.focus();
            return;
        }

        // Set Loading state
        submitBtn.disabled = true;
        btnSpinner.classList.remove('d-none');
        btnIcon.classList.add('d-none');
        btnText.textContent = 'Analyzing with ATS Engine...';

        try {
            const response = await fetch('/ai-resume/quick-scan/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json'
                },
                body: JSON.stringify({
                    resume_text: text,
                    target_role: role
                })
            });

            const result = await response.json();

            if (!response.ok || !result.success) {
                showError(result.error || 'Failed to complete ATS scan. Please try again.');
                return;
            }

            // Render Results
            renderResults(result);

        } catch (err) {
            showError('Network error connecting to ATS analysis engine. Please try again.');
        } finally {
            submitBtn.disabled = false;
            btnSpinner.classList.add('d-none');
            btnIcon.classList.remove('d-none');
            btnText.textContent = 'Run Instant ATS Scan';
        }
    });

    function renderResults(data) {
        // Toggle view states
        if (stateIdle) stateIdle.classList.add('d-none');
        if (stateActive) stateActive.classList.remove('d-none');

        // Header info
        const roleEl = document.getElementById('res-target-role');
        const scoreEl = document.getElementById('res-overall-score');
        const badgeEl = document.getElementById('res-score-badge');

        if (roleEl) roleEl.textContent = data.target_role;
        if (scoreEl) scoreEl.textContent = data.overall_score + '%';

        if (badgeEl) {
            badgeEl.className = 'badge fw-semibold px-2 py-1 rounded';
            if (data.overall_score >= 70) {
                badgeEl.classList.add('bg-success', 'bg-opacity-10', 'text-success');
                badgeEl.textContent = 'Strong Match';
            } else if (data.overall_score >= 50) {
                badgeEl.classList.add('bg-primary', 'bg-opacity-10', 'text-primary');
                badgeEl.textContent = 'Moderate Match';
            } else {
                badgeEl.classList.add('bg-warning', 'bg-opacity-10', 'text-warning');
                badgeEl.textContent = 'Needs Alignment';
            }
        }

        // Metrics grid
        const skillScore = document.getElementById('res-skill-score');
        const jdScore = document.getElementById('res-jd-score');
        const xyzBullets = document.getElementById('res-xyz-bullets');
        const completenessScore = document.getElementById('res-completeness-score');

        if (skillScore) skillScore.textContent = (data.skill_coverage_score || 0) + '%';
        if (jdScore) jdScore.textContent = (data.jd_match_score || 0) + '%';
        if (xyzBullets) xyzBullets.textContent = (data.quantified_bullets_count || 0) + '/' + (data.total_bullets_count || 0);
        if (completenessScore) completenessScore.textContent = (data.completeness_score || 0) + '%';

        // Matched chips
        const matchedWrap = document.getElementById('res-matched-chips');
        const matchedCount = document.getElementById('res-matched-count');
        if (matchedWrap) {
            matchedWrap.innerHTML = '';
            const matched = data.matched_skills || [];
            if (matchedCount) matchedCount.textContent = `${matched.length} matched`;
            if (matched.length > 0) {
                matched.forEach(skill => {
                    const chip = document.createElement('span');
                    chip.className = 'quickscan-chip matched';
                    chip.innerHTML = `<i class="bi bi-check2"></i> ${escapeHtml(skill)}`;
                    matchedWrap.appendChild(chip);
                });
            } else {
                matchedWrap.innerHTML = '<span class="text-muted small">No target role core skills detected in text.</span>';
            }
        }

        // Missing chips
        const missingWrap = document.getElementById('res-missing-chips');
        const missingCount = document.getElementById('res-missing-count');
        if (missingWrap) {
            missingWrap.innerHTML = '';
            const missing = data.missing_skills || [];
            if (missingCount) missingCount.textContent = `${missing.length} missing`;
            if (missing.length > 0) {
                missing.forEach(skill => {
                    const chip = document.createElement('span');
                    chip.className = 'quickscan-chip missing';
                    chip.innerHTML = `<i class="bi bi-exclamation"></i> ${escapeHtml(skill)}`;
                    missingWrap.appendChild(chip);
                });
            } else {
                missingWrap.innerHTML = '<span class="text-success small"><i class="bi bi-check-all"></i> Great job! All core skills present.</span>';
            }
        }

        // Suggestions
        const suggestionsBox = document.getElementById('res-suggestions-list');
        if (suggestionsBox) {
            suggestionsBox.innerHTML = '';
            const suggestions = data.suggestions || [];
            if (suggestions.length > 0) {
                suggestions.forEach(item => {
                    const el = document.createElement('div');
                    el.className = 'quickscan-suggestion-item';
                    el.innerHTML = `<strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.text)}</span>`;
                    suggestionsBox.appendChild(el);
                });
            } else {
                suggestionsBox.innerHTML = '<span class="text-muted small">Your resume structure is well aligned with ATS benchmarks.</span>';
            }
        }

        // Smooth scroll result into view on mobile
        if (window.innerWidth < 992) {
            const resultsCard = document.getElementById('quickscan-results-box');
            if (resultsCard) {
                resultsCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }
    }

    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }
}

