import axios from 'axios';
import { useAuthStore } from '../store/authStore';
import { triggerToast } from '../components/ToastContainer';

const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
});

// Attach JWT to every request
apiClient.interceptors.request.use(
  (config) => {
    const token = useAuthStore.getState()?.token;
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Handle auth errors globally
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status;
    const url = error.config?.url || '';
    if (status === 401 && !url.includes('/auth/login') && !url.includes('/auth/guest') && !url.includes('/auth/logout')) {
      useAuthStore.getState().logout();
      window.location.replace('/login');
    } else if (status === 403) {
      const detail = error.response?.data?.detail || '';
      const isHalt = detail.toLowerCase().includes('halt') || detail.toLowerCase().includes('frozen');
      triggerToast({
        type: 'HALT',
        title: isHalt ? 'EXECUTION LOCKED' : 'ACCESS DENIED',
        message: detail || 'Emergency halt active — all order placement is frozen.',
      });
    }
    return Promise.reject(error);
  }
);

export default apiClient;
