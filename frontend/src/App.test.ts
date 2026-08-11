import { render, screen } from '@testing-library/vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

import App from './App.vue'

describe('App', () => {
  it('renders the WheelForge application marker', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div>WheelForge</div>' } }],
    })
    await router.push('/')
    await router.isReady()
    render(App, { global: { plugins: [createPinia(), router] } })

    expect(screen.getByText('WheelForge')).toBeInTheDocument()
  })
})
