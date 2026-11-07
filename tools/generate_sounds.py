#!/usr/bin/env python3
"""Regenerates every tick and music loop in static/sounds/.

Nothing here uses recordings, samples, loops or machine-learning models. Each sound is
synthesised from sine waves, filtered noise and simple drum models, so there is no
third-party audio to license. Requires numpy and scipy.

Usage: python tools/generate_sounds.py [output_dir]
"""
import sys
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "static" / "sounds"
OUT.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(11)


def save(name, x, sr, peak=.8):
    x = x - x.mean(); x = x / np.abs(x).max() * peak
    w = wave.open(str(OUT / (name + ".wav")), "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes((x * 32767).astype("<i2").tobytes()); w.close()


def add(buf, start, sig, g=1.0):          # wrap-around add so every loop is seamless
    n = len(buf); i = int(start * SR) % n; m = min(len(sig), n - i)
    buf[i:i+m] += g * sig[:m]
    if m < len(sig): buf[:len(sig) - m] += g * sig[m:]


def lp(x, hz, order=2): return sosfilt(butter(order, hz, "low", fs=SR, output="sos"), x)


# ---------- tick loops (mono 22.05 kHz, 8 s) ----------
SR = 22050
def click(parts, noise, ndec, dur=.12):
    t = np.arange(int(SR * dur)) / SR
    y = sum(a * np.sin(2 * np.pi * f * t) * np.exp(-t * d) for f, a, d in parts)
    return y + noise * rng.standard_normal(len(t)) * np.exp(-t * ndec)
def ticks(name, period, parts, noise, ndec, alt=.84, second=0, gain=1.0):
    buf = np.zeros(SR * 8)
    for k in range(int(8 / period)):
        f = 1 if k % 2 == 0 else alt
        p = [(fr * f, a, d) for fr, a, d in parts]
        add(buf, k * period + .03, click(p, noise, ndec))
        if second: add(buf, k * period + .03 + second, click([(fr * f * .8, a, d * 1.3) for fr, a, d in parts], noise, ndec), .45)
    save("tick-" + name, buf, SR, .8 * gain)
ticks("wall", 1.0, [(1900, .7, 220), (3300, .4, 300), (5200, .2, 450)], .5, 900, second=.05)
ticks("wood", 1.0, [(430, 1, 60), (930, .5, 80), (1500, .3, 110)], .25, 500, alt=.88)
ticks("metronome", 1.0, [(1000, 1, 70), (1900, .4, 90), (3100, .25, 140)], .6, 1500, alt=1.0)
ticks("pocket", .2, [(3300, .6, 500), (5400, .4, 650)], .4, 1800, alt=.9, gain=.6)

# ---------- music loops ----------
mt = lambda m: 440 * 2 ** ((m - 69) / 12)
def ep(m, dur, vel=1.0, tail=3.0):
    f = mt(m); t = np.arange(int(SR * (dur + .3))) / SR
    y = np.sin(2*np.pi*f*t)*np.exp(-t*tail) + .4*np.sin(2*np.pi*2*f*t)*np.exp(-t*(tail+2)) + .12*np.sin(2*np.pi*3*f*t)*np.exp(-t*(tail+4))
    y *= (1 + .08 * np.sin(2*np.pi*4.5*t)) * np.minimum(1, t / .006) * np.clip((dur + .3 - t) / .3, 0, 1)
    return vel * y
def kick():
    t = np.arange(int(SR * .3)) / SR; return np.sin(2*np.pi*(55 * t * 0 + 48 + 90 * np.exp(-t * 30)) * t) * np.exp(-t * 14)
def snare():
    t = np.arange(int(SR * .2)) / SR; return (rng.standard_normal(len(t)) * .6 + np.sin(2*np.pi*190*t)) * np.exp(-t * 22)
def hat():
    t = np.arange(int(SR * .05)) / SR; return rng.standard_normal(len(t)) * np.exp(-t * 90)
def lofi(name, bpm, chords, scale, bars=16, snare_on=True, seed=1):
    r = np.random.default_rng(seed); beat = 60 / bpm; N = int(SR * beat * 4 * bars); buf = np.zeros(N)
    for b in range(bars):
        s = b * 4 * beat; ch = chords[(b // 2) % len(chords)] if len(chords) > 2 else chords[b % len(chords)]
        for i, m in enumerate(ch): add(buf, s + i * .012, ep(m, 3.4 * beat, .5, 1.6))
        add(buf, s, ep(ch[0] - 12, 1.6 * beat, .9, 3.5), .8); add(buf, s + 2.5 * beat, ep(ch[0] - 12, 1.2 * beat, .9, 3.5), .6)
        for j in r.choice(16, 4, replace=False):
            add(buf, s + j * beat / 4 + (.03 if j % 2 else 0), ep(int(r.choice(scale)), beat, .35, 2.2), .5)
        for q in (0, 2): add(buf, s + q * beat, kick(), .9)
        if r.random() < .4: add(buf, s + 2.75 * beat, kick(), .5)
        if snare_on:
            for q in (1, 3): add(buf, s + q * beat, snare(), .45)
        for e in range(8): add(buf, s + e * beat / 2 + (.12 * beat if e % 2 else 0), hat(), .12 + .06 * (e % 2 == 0))
    buf = lp(buf, 4200)
    cr = np.zeros(N); idx = r.integers(0, N, N // 900); cr[idx] = r.uniform(-1, 1, len(idx)) * .06
    buf += lp(cr, 6000) + lp(rng.standard_normal(N), 3000) * .004
    save(name, buf, SR, .8)
lofi("music-lofi1", 74, [[50,53,57,60,64],[43,53,59,62,69],[48,52,59,62,67],[45,55,59,60,64]], [72,74,76,79,81,84], seed=3)
lofi("music-lofi2", 66, [[45,52,55,60,64],[41,48,52,57,60],[48,55,59,62,64],[43,50,55,59,62]], [69,72,74,76,79,81], snare_on=False, seed=8)

def fft_shape(N, fn):                       # circular (seamless) coloured noise
    X = np.fft.rfft(rng.standard_normal(N)); f = np.fft.rfftfreq(N, 1 / SR); f[0] = 1
    return np.fft.irfft(X * fn(f), N)
SR = 16000; N = SR * 48; t = np.arange(N) / SR; pad = np.zeros(N)
for m in [48, 55, 62, 64, 71, 72, 79]:
    for det in (-.08, .0, .09):
        f = round(mt(m) * 2 ** (det / 12) * 48) / 48; k = int(rng.integers(1, 4)); ph = rng.uniform(0, 6.28)
        pad += np.sin(2*np.pi*f*t + ph) * (.55 + .45 * np.sin(2*np.pi*k*t/48 + ph)) / (1 + m / 60)
irr = np.exp(-np.arange(SR * 3) / (SR * .9)) * rng.standard_normal(SR * 3); irr = np.pad(irr, (0, N - len(irr)))
pad = np.fft.irfft(np.fft.rfft(pad) * np.fft.rfft(irr) * np.exp(-(np.fft.rfftfreq(N, 1/SR) / 2500) ** 2), N)
save("music-ambient", pad, SR, .55)
brown = fft_shape(N := SR * 20, lambda f: np.where(f < 25, 0, 1 / f) * np.exp(-(f / 900) ** 2)); save("music-brown", brown, SR, .6)
SR = 22050; N = SR * 24
rain = fft_shape(N, lambda f: np.where((f > 300) & (f < 8000), f ** -.4, 0)) * 1.0 + fft_shape(N, lambda f: np.where(f < 200, 1 / (f + 20), 0)) * .5
for _ in range(500):
    t2 = np.arange(int(SR * .02)) / SR; add(rain, rng.uniform(0, 24), np.sin(2*np.pi*rng.uniform(2500, 6500)*t2) * np.exp(-t2 * 380), rng.uniform(.05, .22) * np.abs(rain).max() * 3)
save("music-rain", rain, SR, .6)
print("Wrote 9 sounds to", OUT)
