import { request, type Identity } from './client'

// Session transitions own their failures; a rejected login must keep its error.
export const authApi = {
  me: () => request<Identity>('/auth/me', 'GET', undefined, false),
  login: (username: string, password: string) =>
    request<{ csrf_token: string }>('/auth/login', 'POST', { username, password }, false),
  logout: () => request('/auth/logout', 'POST', undefined, false),
  password: (current_password: string, password: string) =>
    request('/auth/password', 'POST', { current_password, password }),
}
