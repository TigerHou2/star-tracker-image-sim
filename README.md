# Star Tracker Image Simulator

This package simulates star tracker images of distant stars (and hopefully unresolved asteroids/planets in the future) given lens + sensor specifications and camera orientation. 

The effects considered include lens distortion, the point spread function, transmission/filter/quantum efficiencies, shot noise, dark current, read noise, saturation, and bloom. 

[__Check out an example here__](https://html-preview.github.io/?url=https://github.com/TigerHou2/star-tracker-image-sim/blob/main/examples/gaia.html).


## Available Models

### Lens Distortion
- Brown-Conrady

### PSF
- Pillbox
- Gaussian
- Airy
- Defocus (Wyant and Creath)

### Filter/Quantum Efficiency
- Average over designated bandpass

### Dark Current
- Exponential temperature

### Bloom
- Unidirectional/Symmetric bloom along 0, 1, or 2 axes

### Shutter
- Global shutter

