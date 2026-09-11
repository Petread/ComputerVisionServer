import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';

export function LoginPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');

  const loginMutation = useMutation({
    mutationFn: api.admin.login,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['session'] });
      notify({ title: 'Signed in successfully', tone: 'success' });
      navigate({ to: '/' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Login failed'), tone: 'error' });
    }
  });

  return (
    <div className="screen-center login-screen">
      <form
        className="login-card"
        onSubmit={(event) => {
          event.preventDefault();
          loginMutation.mutate({ username, password });
        }}
      >
        <h1>CV Platform</h1>
        <p>Sign in to open the control plane.</p>
        <label>
          Username
          <input value={username} onChange={(event) => setUsername(event.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <button type="submit" disabled={loginMutation.isPending}>
          {loginMutation.isPending ? 'Signing in…' : 'Sign In'}
        </button>
      </form>
    </div>
  );
}
