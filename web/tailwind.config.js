/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  // Theme is token-derived via the generated preset (design-tokens/gen.mjs).
  presets: [require('./tailwind.preset.cjs')],
  theme: { extend: {} },
  plugins: [],
}
