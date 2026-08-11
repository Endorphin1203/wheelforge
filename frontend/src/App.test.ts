import { render, screen } from '@testing-library/vue'

import App from './App.vue'

describe('App', () => {
  it('renders the WheelForge application marker', () => {
    render(App)

    expect(screen.getByText('WheelForge')).toBeInTheDocument()
  })
})
