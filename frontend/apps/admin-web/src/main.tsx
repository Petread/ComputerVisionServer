// Build-time isolation: the local prototype never imports the production API/router.
if (import.meta.env.MODE === 'demo') {
  void import('./demo/bootstrap');
} else {
  void import('./app/bootstrap');
}
