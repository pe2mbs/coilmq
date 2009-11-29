#
# Copyright 2009 Hans Lellelid
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#   http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Authentication providers.

Because authentication providers are instantiated and configured in the application scope
(and not in the request handler), the authenticator implementations must be thread-safe.
"""
__authors__ = [
  '"Hans Lellelid" <hans@xmpl.org>',
]

class AuthError(Exception):
    pass


class Authenticator(object):
    """ Abstract base class for authenticators. """
    
    def authenticate(self, login, passcode):
        """
        Authenticate the login and passcode.
         
        @return: Whether user is authenticated.
        @rtype: C{bool} 
        """
        raise NotImplementedError()