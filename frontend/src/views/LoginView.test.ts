import { render, screen } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

import { apiClient, ApiError } from '@/api/client'
import LoginView from './LoginView.vue'

describe('LoginView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    sessionStorage.clear()
  })

  it('shows an explicit invalid credential message', async () => {
    vi.spyOn(apiClient, 'request').mockRejectedValueOnce(
      new ApiError(401, 'AUTHENTICATION_FAILED', 'Invalid username or password'),
    )
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/login', component: LoginView },
        { path: '/build/new', component: { template: '<div>build</div>' } },
      ],
    })
    await router.push('/login')
    await router.isReady()
    render(LoginView, { global: { plugins: [router, ElementPlus] } })
    const user = userEvent.setup()

    await user.type(screen.getByLabelText('用户名'), 'builder')
    await user.type(screen.getByLabelText('密码'), 'wrong')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByText('用户名或密码错误')).toBeInTheDocument()
  })
})
