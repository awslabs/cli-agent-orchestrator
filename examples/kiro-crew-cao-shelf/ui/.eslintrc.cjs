module.exports = {
  root: true,
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 2022, sourceType: 'module', ecmaFeatures: { jsx: true } },
  plugins: ['react', '@typescript-eslint'],
  extends: ['eslint:recommended', 'plugin:react/recommended', 'plugin:react/jsx-runtime', 'plugin:@typescript-eslint/recommended'],
  settings: { react: { version: '18.2' } },
  env: { browser: true, es2022: true },
  rules: {
    // CAO strings are untrusted: never inject markup.
    'react/no-danger': 'error',
    'react/no-danger-with-children': 'error',
    'react/jsx-no-target-blank': 'error',
    'react/prop-types': 'off',
    // inline styles bypass the host-token rules that styles.test.ts enforces on the stylesheet
    'react/forbid-dom-props': ['error', { forbid: ['style'] }],
  },
}
