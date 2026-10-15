#!/usr/bin/env python3
"""
OSRS DVS Dump Scanner Web Dashboard
===================================
A real-time web application serving an interactive dark-mode dashboard
for finding GE dumps and analyzing backtests.

Powered by Python's built-in http.server.ThreadingHTTPServer.
Default URL: http://127.0.0.1:8080
"""
import argparse
import importlib.util
import json
import math
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

SCRIPT_DIR = Path(__file__).resolve().parent

# Dynamically import "dump DVS scanner.py"
scanner_path = SCRIPT_DIR / "dump DVS scanner.py"
if not scanner_path.exists():
    raise FileNotFoundError(f"Cannot find scanner at {scanner_path}")

spec = importlib.util.spec_from_file_location("dump_dvs_scanner", str(scanner_path))
if spec is None or spec.loader is None:
    raise ImportError(f"Failed to load spec for {scanner_path}")
dvs_scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dvs_scanner)

# Import dvs_backtest
import dvs_backtest  # noqa: E402

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OSRS DVS Dump Scanner & Backtester v2.7</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', 'sans-serif'],
            mono: ['JetBrains Mono', 'monospace'],
          },
          colors: {
            brand: {
              50: '#eff6ff',
              400: '#60a5fa',
              500: '#3b82f6',
              600: '#2563eb',
            },
            osrs: {
              gold: '#eab308',
              amber: '#f59e0b',
              dark: '#0b0f19',
              card: '#111827',
              border: '#1f2937',
              accent: '#374151'
            }
          }
        }
      }
    }
  </script>
  <style>
    body {
      background-color: #0b0f19;
      color: #f3f4f6;
    }
    .custom-scrollbar::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    .custom-scrollbar::-webkit-scrollbar-track {
      background: #111827;
    }
    .custom-scrollbar::-webkit-scrollbar-thumb {
      background: #374151;
      border-radius: 3px;
    }
    .custom-scrollbar::-webkit-scrollbar-thumb:hover {
      background: #4b5563;
    }
    .glow-gold {
      box-shadow: 0 0 15px rgba(234, 179, 8, 0.15);
    }
    .glow-green {
      box-shadow: 0 0 15px rgba(34, 197, 94, 0.2);
    }
    .badge-pulse {
      animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: .5; }
    }
  </style>
</head>
<body class="min-h-screen flex flex-col font-sans antialiased text-slate-100">

  <!-- Header -->
  <header class="border-b border-osrs-border bg-osrs-card/80 backdrop-blur sticky top-0 z-40">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-amber-500/10 border border-amber-500/30 flex items-center justify-center text-amber-400 font-bold text-xl shadow-inner">
          ⚔️
        </div>
        <div>
          <div class="flex items-center space-x-2">
            <h1 class="font-extrabold text-lg text-white tracking-tight">OSRS DVS Scanner</h1>
            <span class="text-xs px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30 font-mono font-semibold">v2.7</span>
          </div>
          <p class="text-xs text-slate-400">Grand Exchange Dump Vulnerability & Bounce Engine</p>
        </div>
      </div>

      <!-- Live Tick & Auto-Watch Status -->
      <div class="flex items-center space-x-4">
        <div class="hidden sm:flex flex-col text-right">
          <span class="text-xs text-slate-400">Next GE 5m Tick in</span>
          <span id="countdown-timer" class="font-mono text-sm font-bold text-amber-400">--:--</span>
        </div>
        
        <button id="btn-autowatch" onclick="toggleAutoWatch()" class="flex items-center space-x-2 px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800 hover:bg-slate-700 text-xs font-semibold transition">
          <span id="autowatch-dot" class="w-2.5 h-2.5 rounded-full bg-slate-500"></span>
          <span id="autowatch-text">Auto-Watch: Off</span>
        </button>

        <button id="btn-audio" onclick="toggleAudio()" title="Sound alerts for STRONG BUY signals" class="p-2 rounded-lg border border-slate-700 bg-slate-800 hover:bg-slate-700 text-slate-300 text-sm transition">
          <span id="audio-icon">🔔</span>
        </button>

        <button id="btn-scan" onclick="runScan()" class="flex items-center space-x-2 px-4 py-2 rounded-lg bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold text-xs uppercase tracking-wider transition shadow-lg shadow-amber-500/20 active:scale-95">
          <svg id="scan-spinner" class="animate-spin -ml-0.5 mr-2 h-4 w-4 text-slate-950 hidden" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>
          </svg>
          <span id="scan-btn-text">Scan Now</span>
        </button>
      </div>
    </div>
  </header>

  <!-- Control & Filter Bar -->
  <div class="border-b border-osrs-border bg-slate-900/50 p-4">
    <div class="max-w-7xl mx-auto">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <!-- Relaxed Mode Toggle & Preset Presets -->
        <div class="flex items-center space-x-4">
          <label class="flex items-center space-x-2 cursor-pointer bg-slate-800/80 px-3 py-2 rounded-lg border border-slate-700 hover:border-slate-600 transition">
            <input type="checkbox" id="check-relaxed" onchange="onRelaxedChanged()" class="w-4 h-4 rounded text-amber-500 focus:ring-amber-400 focus:ring-offset-slate-900 bg-slate-900 border-slate-700">
            <span class="text-xs font-semibold text-slate-200">Relaxed Mode</span>
            <span class="text-[10px] text-amber-400/80 font-mono">(DVS 45, Profit 40k)</span>
          </label>

          <label class="flex items-center space-x-2 cursor-pointer bg-slate-800/80 px-3 py-2 rounded-lg border border-slate-700 hover:border-slate-600 transition">
            <input type="checkbox" id="check-knife" checked class="w-4 h-4 rounded text-amber-500 focus:ring-amber-400 focus:ring-offset-slate-900 bg-slate-900 border-slate-700">
            <span class="text-xs font-semibold text-slate-200">Knife Check</span>
          </label>

          <label class="flex items-center space-x-2 cursor-pointer bg-slate-800/80 px-3 py-2 rounded-lg border border-slate-700 hover:border-slate-600 transition">
            <input type="checkbox" id="check-log" checked class="w-4 h-4 rounded text-amber-500 focus:ring-amber-400 focus:ring-offset-slate-900 bg-slate-900 border-slate-700">
            <span class="text-xs font-semibold text-slate-200">Log Signals</span>
          </label>
        </div>

        <!-- Numeric Parameters -->
        <div class="flex flex-wrap items-center gap-3">
          <div class="flex items-center space-x-1.5 bg-slate-800/60 px-2.5 py-1.5 rounded-lg border border-slate-700">
            <span class="text-[11px] text-slate-400">Min DVS</span>
            <input type="number" id="input-min-dvs" value="55" step="1" min="10" max="100" class="w-14 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5 text-xs font-mono text-center text-amber-300">
          </div>

          <div class="flex items-center space-x-1.5 bg-slate-800/60 px-2.5 py-1.5 rounded-lg border border-slate-700">
            <span class="text-[11px] text-slate-400">Min Profit</span>
            <input type="number" id="input-min-profit" value="80000" step="5000" min="0" class="w-20 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5 text-xs font-mono text-center text-emerald-400">
            <span class="text-[10px] text-slate-500 font-mono">gp</span>
          </div>

          <div class="flex items-center space-x-1.5 bg-slate-800/60 px-2.5 py-1.5 rounded-lg border border-slate-700">
            <span class="text-[11px] text-slate-400">Min Price</span>
            <input type="number" id="input-min-price" value="50" step="10" min="1" class="w-16 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5 text-xs font-mono text-center text-slate-200" title="Block penny items like runes/feathers">
            <span class="text-[10px] text-slate-500 font-mono">gp</span>
          </div>

          <div class="flex items-center space-x-1.5 bg-slate-800/60 px-2.5 py-1.5 rounded-lg border border-slate-700">
            <span class="text-[11px] text-slate-400">Slots</span>
            <input type="number" id="input-slots" placeholder="All" min="1" max="8" class="w-14 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5 text-xs font-mono text-center text-slate-200">
          </div>

          <div class="flex items-center space-x-1.5 bg-slate-800/60 px-2.5 py-1.5 rounded-lg border border-slate-700">
            <span class="text-[11px] text-slate-400">Bankroll</span>
            <input type="number" id="input-bankroll" placeholder="Auto" step="1000000" class="w-24 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5 text-xs font-mono text-center text-slate-200">
          </div>

          <div class="flex items-center space-x-1.5 bg-slate-800/60 px-2.5 py-1.5 rounded-lg border border-slate-700">
            <span class="text-[11px] text-slate-400">Retrace</span>
            <input type="number" id="input-retrace" value="0.75" step="0.05" min="0.1" max="1.0" class="w-14 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5 text-xs font-mono text-center text-slate-200">
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Navigation Tabs -->
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 mt-4 w-full">
    <div class="flex border-b border-osrs-border space-x-6 text-sm font-medium">
      <button id="tab-btn-live" onclick="switchTab('live')" class="pb-3 border-b-2 border-amber-500 text-amber-400 flex items-center space-x-2 transition">
        <span>⚡ Live Opportunities</span>
        <span id="badge-count" class="px-2 py-0.5 text-xs rounded-full bg-amber-500/20 text-amber-300 font-mono">0</span>
      </button>
      <button id="tab-btn-funnel" onclick="switchTab('funnel')" class="pb-3 border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center space-x-2 transition">
        <span>🔍 Rejection Funnel</span>
      </button>
      <button id="tab-btn-backtest" onclick="switchTab('backtest')" class="pb-3 border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center space-x-2 transition">
        <span>📊 Backtest Log</span>
      </button>
    </div>
  </div>

  <!-- Main Content Container -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 w-full flex-1">

    <!-- TAB 1: LIVE OPPORTUNITIES -->
    <section id="tab-live" class="space-y-6">
      <!-- KPI Stats Grid -->
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <div class="bg-osrs-card border border-osrs-border p-4 rounded-xl shadow-sm">
          <p class="text-xs text-slate-400">Strict Signals</p>
          <div class="mt-1 flex items-baseline justify-between">
            <span id="kpi-count" class="text-2xl font-extrabold text-white font-mono">0</span>
            <span id="kpi-strong-badge" class="text-xs px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-mono hidden">0 Strong</span>
          </div>
        </div>

        <div class="bg-osrs-card border border-osrs-border p-4 rounded-xl shadow-sm">
          <p class="text-xs text-slate-400">Total Est. Profit</p>
          <div class="mt-1">
            <span id="kpi-profit" class="text-2xl font-extrabold text-emerald-400 font-mono">0 gp</span>
          </div>
        </div>

        <div class="bg-osrs-card border border-osrs-border p-4 rounded-xl shadow-sm">
          <p class="text-xs text-slate-400">Allocated Capital</p>
          <div class="mt-1">
            <span id="kpi-capital" class="text-2xl font-extrabold text-slate-200 font-mono">0 gp</span>
          </div>
        </div>

        <div class="bg-osrs-card border border-osrs-border p-4 rounded-xl shadow-sm">
          <p class="text-xs text-slate-400">Near-Miss Watchlist</p>
          <div class="mt-1 flex items-baseline justify-between">
            <span id="kpi-nearmiss-count" class="text-2xl font-extrabold text-amber-400 font-mono">0</span>
            <span class="text-xs text-slate-500">runners-up</span>
          </div>
        </div>
      </div>

      <!-- Opportunities Table Card -->
      <div class="bg-osrs-card border border-osrs-border rounded-xl shadow overflow-hidden">
        <div class="px-5 py-4 border-b border-osrs-border flex items-center justify-between">
          <h2 class="font-bold text-sm text-white tracking-wide uppercase">Primary Dump Signals</h2>
          <span id="scan-timestamp" class="text-xs text-slate-500 font-mono">Last scanned: Never</span>
        </div>

        <div class="overflow-x-auto custom-scrollbar">
          <table class="w-full text-left border-collapse text-xs">
            <thead>
              <tr class="border-b border-osrs-border bg-slate-900/60 text-slate-400 font-semibold uppercase tracking-wider text-[11px]">
                <th class="py-3 px-4 cursor-pointer hover:text-white" onclick="sortTable('name')">Item Name</th>
                <th class="py-3 px-3 text-right cursor-pointer hover:text-white" onclick="sortTable('p_dump')">Dump Price</th>
                <th class="py-3 px-3 text-right cursor-pointer hover:text-white" onclick="sortTable('p_exit')">Exit Target</th>
                <th class="py-3 px-3 text-right cursor-pointer hover:text-white" onclick="sortTable('net_roi_pct')">Net ROI</th>
                <th class="py-3 px-3 text-right cursor-pointer hover:text-white" onclick="sortTable('dvs')">DVS</th>
                <th class="py-3 px-3 text-center">Pillars (M/S/D)</th>
                <th class="py-3 px-3 text-right cursor-pointer hover:text-white" onclick="sortTable('est_fill')">Est Fill</th>
                <th class="py-3 px-4 text-right cursor-pointer hover:text-white" onclick="sortTable('est_profit')">Est Profit</th>
                <th class="py-3 px-3 text-center">Status</th>
                <th class="py-3 px-4 text-center">Action</th>
              </tr>
            </thead>
            <tbody id="opportunities-tbody" class="divide-y divide-osrs-border font-mono text-slate-200">
              <tr>
                <td colspan="10" class="py-8 text-center text-slate-500 font-sans">
                  Click <strong>Scan Now</strong> to fetch live dumps from the OSRS Wiki API.
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Near-Miss Watchlist Card -->
      <div id="nearmiss-card" class="bg-osrs-card border border-osrs-border rounded-xl shadow overflow-hidden">
        <div class="px-5 py-3.5 border-b border-osrs-border bg-amber-500/5 flex items-center justify-between">
          <div class="flex items-center space-x-2">
            <span class="text-amber-400 text-sm">⚠️</span>
            <h3 class="font-bold text-xs uppercase tracking-wider text-amber-300">Top Near-Miss Watchlist</h3>
            <span class="text-[11px] text-slate-400 font-sans">(Passed baseline & tax, slightly below strict score/profit thresholds)</span>
          </div>
          <span class="text-xs text-slate-500 font-mono">Top 5</span>
        </div>

        <div class="overflow-x-auto custom-scrollbar">
          <table class="w-full text-left border-collapse text-xs">
            <thead>
              <tr class="border-b border-osrs-border bg-slate-900/60 text-slate-400 font-semibold uppercase tracking-wider text-[11px]">
                <th class="py-2.5 px-4">Item Name</th>
                <th class="py-2.5 px-3 text-right">Dump Price</th>
                <th class="py-2.5 px-3 text-right">Exit Target</th>
                <th class="py-2.5 px-3 text-right">Net ROI</th>
                <th class="py-2.5 px-3 text-right">DVS</th>
                <th class="py-2.5 px-3 text-center">Pillars (M/S/D)</th>
                <th class="py-2.5 px-3 text-right">Est Fill</th>
                <th class="py-2.5 px-4 text-right">Est Profit</th>
                <th class="py-2.5 px-3 text-center">Status</th>
                <th class="py-2.5 px-4 text-center">Action</th>
              </tr>
            </thead>
            <tbody id="nearmiss-tbody" class="divide-y divide-osrs-border font-mono text-slate-300">
              <tr>
                <td colspan="10" class="py-6 text-center text-slate-500 font-sans">No near misses to show yet.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- TAB 2: REJECTION FUNNEL -->
    <section id="tab-funnel" class="space-y-6 hidden">
      <div class="bg-osrs-card border border-osrs-border rounded-xl p-6 shadow">
        <h2 class="font-bold text-base text-white mb-2">Rejection Funnel Analytics</h2>
        <p class="text-xs text-slate-400 mb-6">Real-time inspection of why items were rejected during the scan pass.</p>

        <div id="funnel-container" class="space-y-3">
          <p class="text-slate-500 text-xs">Run a scan first to populate funnel analytics.</p>
        </div>
      </div>
    </section>

    <!-- TAB 3: BACKTEST LOG -->
    <section id="tab-backtest" class="space-y-6 hidden">
      <!-- Backtest Controls -->
      <div class="bg-osrs-card border border-osrs-border rounded-xl p-5 shadow">
        <div class="flex flex-wrap items-center justify-between gap-4">
          <div class="flex flex-wrap items-center gap-4">
            <div>
              <label class="block text-[11px] text-slate-400 mb-1">Horizons (minutes)</label>
              <input type="text" id="bt-horizons" value="15,30,60" class="bg-slate-900 border border-slate-700 rounded px-3 py-1.5 text-xs font-mono text-white w-32">
            </div>

            <div>
              <label class="block text-[11px] text-slate-400 mb-1">Unhit Scratch Mode</label>
              <select id="bt-scratch" class="bg-slate-900 border border-slate-700 rounded px-3 py-1.5 text-xs font-sans text-white">
                <option value="mid" selected>Midpoint (Spread Avg)</option>
                <option value="low">Low (Conservative Instasell)</option>
                <option value="high">High (Optimistic Instabuy)</option>
              </select>
            </div>

            <div class="flex items-center pt-5">
              <label class="flex items-center space-x-2 cursor-pointer">
                <input type="checkbox" id="bt-vol-fills" class="w-4 h-4 rounded text-amber-500 focus:ring-amber-400 bg-slate-900 border-slate-700">
                <span class="text-xs font-semibold text-slate-200">Volume-Constrained Fills</span>
              </label>
            </div>
          </div>

          <button onclick="runBacktest()" class="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-bold text-xs uppercase tracking-wider transition shadow-lg shadow-blue-600/20 active:scale-95 flex items-center space-x-2">
            <svg id="bt-spinner" class="animate-spin -ml-0.5 mr-2 h-4 w-4 text-white hidden" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>
            </svg>
            <span id="bt-btn-text">Execute Backtest</span>
          </button>
        </div>
      </div>

      <!-- Backtest Results Display -->
      <div id="bt-results-container" class="space-y-6">
        <div class="bg-osrs-card border border-osrs-border rounded-xl p-8 text-center text-slate-500 text-xs">
          Configure parameters above and click <strong>Execute Backtest</strong> to evaluate logged signals.
        </div>
      </div>
    </section>

  </main>

  <!-- Notification Toast -->
  <div id="toast" class="fixed bottom-5 right-5 bg-slate-800 border border-slate-700 text-white text-xs px-4 py-2.5 rounded-lg shadow-xl opacity-0 transition-opacity duration-300 pointer-events-none z-50">
    Copied to clipboard!
  </div>

  <script>
    // State
    let currentCandidates = [];
    let currentNearMisses = [];
    let currentFunnel = {};
    let sortColumn = 'dvs';
    let sortAsc = false;
    let autoWatchActive = false;
    let audioAlerts = true;
    let countdownInterval = null;
    let nextBucketSeconds = 0;
    let targetCountdownTime = 0;
    let knownSignalIds = new Set();
    let audioCtx = null;

    // Audio Chime via Web Audio API
    function playChime() {
      if (!audioAlerts) return;
      try {
        if (!audioCtx) {
          audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (audioCtx.state === 'suspended') {
          audioCtx.resume();
        }
        const now = audioCtx.currentTime;
        // Two-tone rising chime (OSRS style ping)
        const osc1 = audioCtx.createOscillator();
        const osc2 = audioCtx.createOscillator();
        const gain = audioCtx.createGain();

        osc1.type = 'sine';
        osc1.frequency.setValueAtTime(587.33, now); // D5
        osc1.frequency.exponentialRampToValueAtTime(880.00, now + 0.15); // A5

        osc2.type = 'triangle';
        osc2.frequency.setValueAtTime(880.00, now);
        osc2.frequency.exponentialRampToValueAtTime(1174.66, now + 0.25); // D6

        gain.gain.setValueAtTime(0.15, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.5);

        osc1.connect(gain);
        osc2.connect(gain);
        gain.connect(audioCtx.destination);

        osc1.start(now);
        osc2.start(now + 0.08);
        osc1.stop(now + 0.5);
        osc2.stop(now + 0.5);
      } catch (e) {
        console.warn('Audio chime failed:', e);
      }
    }

    function toggleAudio() {
      audioAlerts = !audioAlerts;
      document.getElementById('audio-icon').textContent = audioAlerts ? '🔔' : '🔕';
      if (audioAlerts) {
        playChime();
        showToast('Sound alerts enabled');
      } else {
        showToast('Sound alerts muted');
      }
    }

    function showToast(msg) {
      const toast = document.getElementById('toast');
      toast.textContent = msg;
      toast.classList.remove('opacity-0');
      toast.classList.add('opacity-100');
      setTimeout(() => {
        toast.classList.remove('opacity-100');
        toast.classList.add('opacity-0');
      }, 2000);
    }

    function copyItemName(name, event) {
      if (event) event.stopPropagation();
      navigator.clipboard.writeText(name).then(() => {
        showToast(`Copied "${name}" for GE search`);
      }).catch(() => {
        showToast('Clipboard access denied');
      });
    }

    function switchTab(tab) {
      const tabs = ['live', 'funnel', 'backtest'];
      tabs.forEach(t => {
        const sec = document.getElementById(`tab-${t}`);
        const btn = document.getElementById(`tab-btn-${t}`);
        if (t === tab) {
          sec.classList.remove('hidden');
          btn.className = 'pb-3 border-b-2 border-amber-500 text-amber-400 flex items-center space-x-2 transition font-bold';
        } else {
          sec.classList.add('hidden');
          btn.className = 'pb-3 border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center space-x-2 transition';
        }
      });
    }

    function onRelaxedChanged() {
      const isRelaxed = document.getElementById('check-relaxed').checked;
      const minDvsInput = document.getElementById('input-min-dvs');
      const minProfitInput = document.getElementById('input-min-profit');

      if (isRelaxed) {
        minDvsInput.value = '45';
        minProfitInput.value = '40000';
      } else {
        minDvsInput.value = '55';
        minProfitInput.value = '80000';
      }
    }

    function toggleAutoWatch() {
      autoWatchActive = !autoWatchActive;
      const dot = document.getElementById('autowatch-dot');
      const text = document.getElementById('autowatch-text');
      const btn = document.getElementById('btn-autowatch');

      if (autoWatchActive) {
        dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 badge-pulse';
        text.textContent = 'Auto-Watch: On';
        btn.classList.add('border-emerald-500/50', 'bg-emerald-950/30', 'text-emerald-300');
        showToast('Auto-watch activated: scanning on bucket boundaries');
        // If countdown expired or unset, scan now
        if (nextBucketSeconds <= 0) {
          runScan();
        }
      } else {
        dot.className = 'w-2.5 h-2.5 rounded-full bg-slate-500';
        text.textContent = 'Auto-Watch: Off';
        btn.classList.remove('border-emerald-500/50', 'bg-emerald-950/30', 'text-emerald-300');
        showToast('Auto-watch stopped');
      }
    }

    function startCountdown(seconds) {
      nextBucketSeconds = Math.max(1, Math.round(seconds));
      targetCountdownTime = Date.now() + nextBucketSeconds * 1000;
      if (countdownInterval) clearInterval(countdownInterval);

      countdownInterval = setInterval(() => {
        const remaining = Math.max(0, Math.round((targetCountdownTime - Date.now()) / 1000));
        const m = Math.floor(remaining / 60);
        const s = remaining % 60;
        document.getElementById('countdown-timer').textContent = `${m}:${s < 10 ? '0' : ''}${s}`;

        if (remaining <= 0) {
          clearInterval(countdownInterval);
          if (autoWatchActive) {
            runScan();
          }
        }
      }, 1000);
    }

    async function runScan() {
      const spinner = document.getElementById('scan-spinner');
      const btnText = document.getElementById('scan-btn-text');
      spinner.classList.remove('hidden');
      btnText.textContent = 'Scanning...';

      const minDvs = document.getElementById('input-min-dvs').value;
      const minProfit = document.getElementById('input-min-profit').value;
      const minPrice = document.getElementById('input-min-price').value;
      const slots = document.getElementById('input-slots').value;
      const bankroll = document.getElementById('input-bankroll').value;
      const retrace = document.getElementById('input-retrace').value;
      const relaxed = document.getElementById('check-relaxed').checked;
      const knife = document.getElementById('check-knife').checked;
      const log = document.getElementById('check-log').checked;

      const params = new URLSearchParams({
        min_dvs: minDvs,
        min_profit: minProfit,
        min_price: minPrice,
        retrace: retrace,
        relaxed: relaxed ? '1' : '0',
        knife_check: knife ? '1' : '0',
        log: log ? '1' : '0'
      });
      if (slots) params.append('slots', slots);
      if (bankroll) params.append('bankroll', bankroll);

      try {
        const res = await fetch(`/api/scan?${params.toString()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        currentCandidates = data.candidates || [];
        currentNearMisses = data.near_misses || [];
        currentFunnel = data.funnel || {};

        document.getElementById('scan-timestamp').textContent = 'Last scanned: ' + new Date().toLocaleTimeString();
        startCountdown(data.next_bucket_wait_sec || 300);

        // Check for new STRONG BUY
        let hasNewStrong = false;
        currentCandidates.forEach(c => {
          if (c.status.includes('STRONG BUY') && !knownSignalIds.has(c.item_id)) {
            hasNewStrong = true;
          }
          knownSignalIds.add(c.item_id);
        });

        if (hasNewStrong) {
          playChime();
        }

        renderOpportunities();
        renderFunnel();
      } catch (err) {
        console.error('Scan error:', err);
        showToast('Scan failed: ' + err.message);
      } finally {
        spinner.classList.add('hidden');
        btnText.textContent = 'Scan Now';
      }
    }

    function sortTable(col) {
      if (sortColumn === col) {
        sortAsc = !sortAsc;
      } else {
        sortColumn = col;
        sortAsc = false;
      }
      renderOpportunities();
    }

    function renderOpportunities() {
      // Sort candidates
      currentCandidates.sort((a, b) => {
        let valA = a[sortColumn];
        let valB = b[sortColumn];
        if (typeof valA === 'string') {
          return sortAsc ? valA.localeCompare(valB) : valB.localeCompare(valA);
        }
        return sortAsc ? valA - valB : valB - valA;
      });

      // Update KPI
      const count = currentCandidates.length;
      document.getElementById('kpi-count').textContent = count;
      document.getElementById('badge-count').textContent = count;
      const strongCount = currentCandidates.filter(c => c.status.includes('STRONG BUY')).length;
      const strongBadge = document.getElementById('kpi-strong-badge');
      if (strongCount > 0) {
        strongBadge.textContent = `${strongCount} Strong`;
        strongBadge.classList.remove('hidden');
      } else {
        strongBadge.classList.add('hidden');
      }

      const totalProfit = currentCandidates.reduce((sum, c) => sum + (c.est_profit || 0), 0);
      document.getElementById('kpi-profit').textContent = totalProfit.toLocaleString() + ' gp';

      const totalCapital = currentCandidates.reduce((sum, c) => sum + ((c.p_dump || 0) * (c.est_fill || 0)), 0);
      document.getElementById('kpi-capital').textContent = totalCapital.toLocaleString() + ' gp';

      document.getElementById('kpi-nearmiss-count').textContent = currentNearMisses.length;

      // Render Primary Table
      const tbody = document.getElementById('opportunities-tbody');
      tbody.innerHTML = '';

      if (count === 0) {
        tbody.innerHTML = `
          <tr>
            <td colspan="10" class="py-8 text-center text-slate-400 font-sans">
              <div class="max-w-md mx-auto space-y-2">
                <p class="font-medium text-slate-300">No strict qualifying dumps found on this tick.</p>
                <p class="text-xs text-slate-500">Market conditions are quiet or dumps did not break support. Check the <strong>Near-Miss Watchlist</strong> below, or toggle <strong>Relaxed Mode</strong>.</p>
              </div>
            </td>
          </tr>
        `;
      } else {
        currentCandidates.forEach(c => {
          const row = document.createElement('tr');
          row.className = 'hover:bg-slate-800/40 transition border-b border-osrs-border/50';

          const isStrong = c.status.includes('STRONG BUY');
          const statusClass = isStrong ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/40 font-bold' : 'bg-amber-500/20 text-amber-300 border border-amber-500/40';
          const alchBadge = c.alch_bonus > 0 ? `<span class="ml-1 text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 border border-purple-500/40">ALCH</span>` : '';

          row.innerHTML = `
            <td class="py-3 px-4 font-sans font-semibold text-white flex items-center space-x-2">
              <a href="https://prices.runescape.wiki/osrs/item/${c.item_id}" target="_blank" rel="noopener" class="hover:text-amber-400 transition hover:underline" title="View live OSRS Wiki page">
                ${c.name}
              </a>
              ${alchBadge}
            </td>
            <td class="py-3 px-3 text-right text-slate-200">${c.p_dump.toLocaleString()}</td>
            <td class="py-3 px-3 text-right text-slate-400">${c.p_exit.toLocaleString()}</td>
            <td class="py-3 px-3 text-right text-emerald-400 font-bold">+${c.net_roi_pct.toFixed(2)}%</td>
            <td class="py-3 px-3 text-right font-extrabold ${c.dvs >= 75 ? 'text-emerald-400' : 'text-amber-400'}">${c.dvs.toFixed(1)}</td>
            <td class="py-3 px-3 text-center text-xs text-slate-400">${c.m_score.toFixed(1)} / ${c.s_score.toFixed(1)} / ${c.d_score.toFixed(1)}</td>
            <td class="py-3 px-3 text-right text-slate-300">${c.est_fill.toLocaleString()}</td>
            <td class="py-3 px-4 text-right font-bold text-emerald-300">${c.est_profit.toLocaleString()} gp</td>
            <td class="py-3 px-3 text-center">
              <span class="inline-block text-[10px] px-2 py-0.5 rounded-full ${statusClass}">${c.status}</span>
            </td>
            <td class="py-3 px-4 text-center">
              <button onclick="copyItemName('${c.name.replace(/'/g, "\\'")}', event)" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white text-[11px] font-sans transition border border-slate-700" title="Copy item name for GE">
                📋 Copy
              </button>
            </td>
          `;
          tbody.appendChild(row);
        });
      }

      // Render Near Misses Table
      const nmBody = document.getElementById('nearmiss-tbody');
      nmBody.innerHTML = '';
      if (currentNearMisses.length === 0) {
        nmBody.innerHTML = `<tr><td colspan="10" class="py-4 text-center text-slate-500 font-sans">No near misses on this tick.</td></tr>`;
      } else {
        currentNearMisses.forEach(c => {
          const row = document.createElement('tr');
          row.className = 'hover:bg-slate-800/40 transition border-b border-osrs-border/30';
          row.innerHTML = `
            <td class="py-2.5 px-4 font-sans text-slate-200">
              <a href="https://prices.runescape.wiki/osrs/item/${c.item_id}" target="_blank" rel="noopener" class="hover:text-amber-400 transition hover:underline">
                ${c.name}
              </a>
            </td>
            <td class="py-2.5 px-3 text-right text-slate-300">${c.p_dump.toLocaleString()}</td>
            <td class="py-2.5 px-3 text-right text-slate-400">${c.p_exit.toLocaleString()}</td>
            <td class="py-2.5 px-3 text-right text-emerald-400">+${c.net_roi_pct.toFixed(2)}%</td>
            <td class="py-2.5 px-3 text-right text-amber-300 font-bold">${c.dvs.toFixed(1)}</td>
            <td class="py-2.5 px-3 text-center text-slate-400">${c.m_score.toFixed(1)} / ${c.s_score.toFixed(1)} / ${c.d_score.toFixed(1)}</td>
            <td class="py-2.5 px-3 text-right text-slate-300">${c.est_fill.toLocaleString()}</td>
            <td class="py-2.5 px-4 text-right text-amber-300 font-semibold">${c.est_profit.toLocaleString()} gp</td>
            <td class="py-2.5 px-3 text-center">
              <span class="text-[10px] px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-slate-700">NEAR MISS</span>
            </td>
            <td class="py-2.5 px-4 text-center">
              <button onclick="copyItemName('${c.name.replace(/'/g, "\\'")}', event)" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white text-[11px] font-sans transition border border-slate-700">
                📋 Copy
              </button>
            </td>
          `;
          nmBody.appendChild(row);
        });
      }
    }

    function renderFunnel() {
      const container = document.getElementById('funnel-container');
      container.innerHTML = '';
      const totalScanned = currentFunnel['scanned'] || 0;
      if (totalScanned === 0) {
        container.innerHTML = '<p class="text-slate-500 text-xs">No scan data available.</p>';
        return;
      }

      const entries = Object.entries(currentFunnel)
        .filter(([k]) => k !== 'scanned')
        .sort((a, b) => b[1] - a[1]);

      let html = `
        <div class="mb-4 bg-slate-800/60 p-4 rounded-lg flex items-center justify-between border border-slate-700">
          <div>
            <span class="text-xs text-slate-400">Total Items Evaluated</span>
            <p class="text-xl font-bold font-mono text-white">${totalScanned.toLocaleString()}</p>
          </div>
          <div>
            <span class="text-xs text-slate-400">Accepted Candidates</span>
            <p class="text-xl font-bold font-mono text-emerald-400">${(currentFunnel['accepted'] || 0).toLocaleString()}</p>
          </div>
        </div>
        <div class="space-y-2">
      `;

      entries.forEach(([label, count]) => {
        const pct = ((count / totalScanned) * 100).toFixed(1);
        const isRej = label.startsWith('rej:');
        const color = isRej ? 'bg-rose-500/80' : (label === 'accepted' ? 'bg-emerald-500' : 'bg-blue-500/80');

        html += `
          <div class="space-y-1">
            <div class="flex justify-between text-xs font-mono">
              <span class="${isRej ? 'text-slate-300' : 'text-emerald-400 font-bold'}">${label}</span>
              <span class="text-slate-400">${count.toLocaleString()} <span class="text-slate-500">(${pct}%)</span></span>
            </div>
            <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
              <div class="${color} h-1.5 rounded-full" style="width: ${Math.min(100, Math.max(1, pct))}%"></div>
            </div>
          </div>
        `;
      });
      html += '</div>';
      container.innerHTML = html;
    }

    async function runBacktest() {
      const spinner = document.getElementById('bt-spinner');
      const btnText = document.getElementById('bt-btn-text');
      spinner.classList.remove('hidden');
      btnText.textContent = 'Backtesting...';

      const horizons = document.getElementById('bt-horizons').value;
      const scratch = document.getElementById('bt-scratch').value;
      const volFills = document.getElementById('bt-vol-fills').checked;

      const params = new URLSearchParams({
        horizons: horizons,
        scratch_mode: scratch,
        volume_fills: volFills ? '1' : '0'
      });

      try {
        const res = await fetch(`/api/backtest?${params.toString()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        renderBacktestResults(json.data);
      } catch (err) {
        console.error('Backtest error:', err);
        showToast('Backtest error: ' + err.message);
      } finally {
        spinner.classList.add('hidden');
        btnText.textContent = 'Execute Backtest';
      }
    }

    function renderBacktestResults(data) {
      const container = document.getElementById('bt-results-container');
      if (!data || data.signals_count === 0) {
        container.innerHTML = `
          <div class="bg-osrs-card border border-osrs-border rounded-xl p-6 text-center text-slate-400 text-xs">
            No valid signals found in <code>dvs_candidate_log.csv</code>. Run the scanner to log signals first!
          </div>
        `;
        return;
      }

      let html = `
        <div class="bg-osrs-card border border-osrs-border rounded-xl p-5 shadow space-y-4">
          <div class="flex items-center justify-between border-b border-osrs-border pb-3">
            <div>
              <h3 class="font-bold text-white text-sm">Historical Backtest Analysis</h3>
              <p class="text-xs text-slate-400 font-mono">Loaded ${data.signals_count} unique signals (${data.skipped} skipped)</p>
            </div>
          </div>
      `;

      Object.entries(data.horizons || {}).forEach(([horizon, hData]) => {
        const summary = hData.summary || {};
        const profitClass = (summary.total_profit || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400';

        html += `
          <div class="border border-slate-800 bg-slate-900/60 rounded-xl p-5 space-y-4">
            <div class="flex items-center justify-between">
              <span class="text-xs font-bold uppercase tracking-wider text-amber-400 bg-amber-500/10 px-2.5 py-1 rounded border border-amber-500/20">
                Horizon: ${horizon} Minutes
              </span>
              <span class="font-mono text-sm font-bold ${profitClass}">
                Total Realized Profit: ${Math.round(summary.total_profit || 0).toLocaleString()} gp
              </span>
            </div>

            <!-- Horizon KPI Grid -->
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono text-xs">
              <div class="bg-slate-800/60 p-3 rounded-lg border border-slate-700/60">
                <span class="text-slate-400 text-[11px] block">Fill Rate</span>
                <span class="text-base font-bold text-white">${(summary.fill_rate || 0).toFixed(1)}%</span>
              </div>
              <div class="bg-slate-800/60 p-3 rounded-lg border border-slate-700/60">
                <span class="text-slate-400 text-[11px] block">Target Hit Rate</span>
                <span class="text-base font-bold text-emerald-400">${(summary.hit_rate || 0).toFixed(1)}%</span>
              </div>
              <div class="bg-slate-800/60 p-3 rounded-lg border border-slate-700/60">
                <span class="text-slate-400 text-[11px] block">Win Rate</span>
                <span class="text-base font-bold text-emerald-300">${(summary.win_rate || 0).toFixed(1)}%</span>
              </div>
              <div class="bg-slate-800/60 p-3 rounded-lg border border-slate-700/60">
                <span class="text-slate-400 text-[11px] block">Avg Net ROI</span>
                <span class="text-base font-bold ${(summary.avg_roi || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}">
                  ${(summary.avg_roi || 0) >= 0 ? '+' : ''}${(summary.avg_roi || 0).toFixed(2)}%
                </span>
              </div>
            </div>

            <!-- Target Sweep Table -->
            ${hData.sweeps && hData.sweeps.length ? `
              <div class="mt-4">
                <h4 class="text-xs font-semibold text-slate-300 mb-2">Retracement Target Sweep</h4>
                <div class="overflow-x-auto custom-scrollbar">
                  <table class="w-full text-xs font-mono text-left border-collapse">
                    <thead>
                      <tr class="text-slate-500 border-b border-slate-800 text-[11px]">
                        <th class="py-2 px-3">Target</th>
                        <th class="py-2 px-3 text-right">Filled Signals</th>
                        <th class="py-2 px-3 text-right">Hit Rate</th>
                        <th class="py-2 px-3 text-right">Avg ROI</th>
                        <th class="py-2 px-3 text-right">Total Profit</th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-800/60">
                      ${hData.sweeps.map(sw => `
                        <tr class="${sw.is_current ? 'bg-amber-500/10 text-amber-300 font-bold' : 'text-slate-300 hover:bg-slate-800/30'}">
                          <td class="py-1.5 px-3">${sw.target.toFixed(2)}${sw.is_current ? ' (Current)' : ''}</td>
                          <td class="py-1.5 px-3 text-right">${sw.signals}</td>
                          <td class="py-1.5 px-3 text-right">${sw.hit_rate.toFixed(1)}%</td>
                          <td class="py-1.5 px-3 text-right ${sw.avg_roi >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${sw.avg_roi >= 0 ? '+' : ''}${sw.avg_roi.toFixed(2)}%</td>
                          <td class="py-1.5 px-3 text-right ${sw.total_profit >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${Math.round(sw.total_profit).toLocaleString()} gp</td>
                        </tr>
                      `).join('')}
                    </tbody>
                  </table>
                </div>
              </div>
            ` : ''}
          </div>
        `;
      });

      html += '</div>';
      container.innerHTML = html;
    }

    // Initial page load: trigger first scan automatically
    window.addEventListener('DOMContentLoaded', () => {
      runScan();
    });
  </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard request log lines in terminal
        pass

    def send_json_response(self, data: Any, status_code: int = 200) -> None:
        payload = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            payload = HTML_TEMPLATE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        elif path == "/api/scan":
            try:
                relaxed = qs.get("relaxed", ["0"])[0].lower() in ("1", "true", "yes")

                default_dvs = 45.0 if relaxed else dvs_scanner.MIN_DVS_SCORE
                default_profit = 40_000 if relaxed else dvs_scanner.MIN_EST_PROFIT
                default_drop = 2.0 if relaxed else dvs_scanner.MIN_RAW_DROP_PCT
                default_margin = 5.0 if relaxed else dvs_scanner.MIN_MARGIN_PILLAR

                min_dvs = float(qs.get("min_dvs", [default_dvs])[0])
                min_profit = int(qs.get("min_profit", [default_profit])[0])
                max_capital = (
                    int(qs.get("max_capital")[0])
                    if qs.get("max_capital")
                    else dvs_scanner.MAX_SLOT_CAPITAL
                )
                min_price = int(qs.get("min_price", [dvs_scanner.MIN_ITEM_PRICE])[0])
                retrace = float(qs.get("retrace", [dvs_scanner.RETRACEMENT_TARGET])[0])

                slots_val = qs.get("slots", [None])[0]
                slots = int(slots_val) if (slots_val and slots_val.isdigit()) else None

                bankroll_val = qs.get("bankroll", [None])[0]
                bankroll = (
                    int(bankroll_val) if (bankroll_val and bankroll_val.isdigit()) else None
                )

                knife_check = qs.get("knife_check", ["1"])[0].lower() in ("1", "true", "yes")
                log_enabled = qs.get("log", ["1"])[0].lower() in ("1", "true", "yes")

                session = dvs_scanner.build_session()
                near_misses: List[Any] = []
                try:
                    candidates, funnel = dvs_scanner.scan_dumps(
                        session,
                        knife_check_enabled=knife_check,
                        max_slots=slots,
                        bankroll=bankroll,
                        near_misses_out=near_misses,
                        min_dvs_score=min_dvs,
                        min_margin_pillar=default_margin,
                        min_demand_pillar=dvs_scanner.MIN_DEMAND_PILLAR,
                        min_est_profit=min_profit,
                        max_slot_capital=max_capital,
                        min_raw_drop_pct=default_drop,
                        min_support_break_pct=dvs_scanner.MIN_SUPPORT_BREAK_PCT,
                        min_item_price=min_price,
                        retracement_target=retrace,
                        max_recovery_fraction=dvs_scanner.MAX_RECOVERY_FRACTION,
                    )
                finally:
                    session.close()

                if log_enabled and candidates:
                    dvs_scanner.log_candidates(candidates)

                wait_sec = dvs_scanner.seconds_until_next_bucket()
                response = {
                    "status": "success",
                    "scan_time": datetime.now(timezone.utc).isoformat(),
                    "candidates": [asdict(c) for c in candidates],
                    "near_misses": [asdict(c) for c in near_misses],
                    "funnel": dict(funnel),
                    "next_bucket_wait_sec": wait_sec,
                }
                self.send_json_response(response)
            except Exception as exc:
                self.send_json_response({"status": "error", "message": str(exc)}, status_code=500)
            return

        elif path == "/api/backtest":
            try:
                horizons_raw = qs.get("horizons", ["15,30,60"])[0]
                horizons = [
                    int(h.strip())
                    for h in horizons_raw.split(",")
                    if h.strip() and h.strip().isdigit()
                ]
                if not horizons:
                    horizons = [30, 60]

                scratch_mode = qs.get("scratch_mode", ["mid"])[0]
                volume_fills = qs.get("volume_fills", ["0"])[0].lower() in (
                    "1",
                    "true",
                    "yes",
                )
                target = float(qs.get("target", ["0.75"])[0])

                results = dvs_backtest.run_backtest(
                    horizons=horizons,
                    target=target,
                    scratch_mode=scratch_mode,
                    volume_fills=volume_fills,
                )
                self.send_json_response({"status": "success", "data": results})
            except Exception as exc:
                self.send_json_response({"status": "error", "message": str(exc)}, status_code=500)
            return

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")


def run_server(host: str = "127.0.0.1", port: int = 8080) -> None:
    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, DashboardRequestHandler)
    print("=" * 70)
    print(f"OSRS DVS SCANNER & BACKTESTER WEB DASHBOARD v2.7".center(70))
    print("=" * 70)
    print(f"Dashboard running at: http://{host}:{port}/")
    print(f"Serving API endpoints: /api/scan, /api/backtest")
    print("Press Ctrl+C to terminate the web server.")
    print("=" * 70)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard server...")
    finally:
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="OSRS DVS Dump Scanner Web Dashboard")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
