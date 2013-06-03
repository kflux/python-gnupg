# -*- coding: utf-8 -*-
#
# This file is part of python-gnupg, a Python interface to GnuPG.
# Copyright © 2013 Isis Lovecruft
#           © 2008-2012 Vinay Sajip
#           © 2005 Steve Traugott
#           © 2004 A.M. Kuchling
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
"""gnupg.py
===========
A Python interface to GnuPG.

This is a modified version of python-gnupg-0.3.2, which was created by Vinay
Sajip, which itself is a modification of GPG.py written by Steve Traugott,
which in turn is a modification of the pycrypto GnuPG interface written by
A.M. Kuchling.

This version is patched to sanitize untrusted inputs, due to the necessity of
executing :class:`subprocess.Popen([...], shell=True)` in order to communicate
with GnuPG. Several speed improvements were also made based on code profiling,
and the API has been cleaned up to support an easier, more Pythonic,
interaction.

:authors: see ``gnupg.__authors__``
:license: see ``gnupg.__license__``
:info:    see <https://www.github.com/isislovecruft/python-gnupg>
"""

from __future__ import absolute_import
from codecs     import open as open

import encodings
import os
import textwrap

try:
    from io import StringIO
except ImportError:
    from cStringIO import StringIO

## see PEP-328 http://docs.python.org/2.5/whatsnew/pep-328.html
from .         import _parsers
from .         import _util
from ._meta    import GPGBase
from ._parsers import _fix_unsafe
from ._util    import _is_list_or_tuple
from ._util    import _is_stream
from ._util    import _make_binary_stream
from ._util    import log


class GPG(GPGBase):
    """Encapsulate access to the gpg executable"""

    #: The number of simultaneous keyids we should list operations like
    #: '--list-sigs' to:
    _batch_limit    = 25

    def __init__(self, binary=None, homedir=None, verbose=False,
                 use_agent=False, keyring=None, secring=None,
                 options=None):
        """Initialize a GnuPG process wrapper.

        :param str binary: Name for GnuPG binary executable. If the absolute
                           path is not given, the evironment variable $PATH is
                           searched for the executable and checked that the
                           real uid/gid of the user has sufficient permissions.

        :param str homedir: Full pathname to directory containing the public
                            and private keyrings. Default is whatever GnuPG
                            defaults to.

        :param str keyring: Name of keyring file containing public key data, if
                            unspecified, defaults to 'pubring.gpg' in the
                            ``homedir`` directory.

        :param str secring: Name of alternative secret keyring file to use. If
                            left unspecified, this will default to using
                            'secring.gpg' in the :param:homedir directory, and
                            create that file if it does not exist.

        :param list options: A list of additional options to pass to the GPG
                             binary.

        :raises: :exc:`RuntimeError` with explanation message if there is a
                 problem invoking gpg.

        Example:

        >>> import gnupg
        GnuPG logging disabled...
        >>> gpg = gnupg.GPG(homedir='./test-homedir')
        >>> gpg.keyring
        './test-homedir/pubring.gpg'
        >>> gpg.secring
        './test-homedir/secring.gpg'
        >>> gpg.use_agent
        False
        >>> gpg.binary
        '/usr/bin/gpg'
        >>> import os
        >>> import shutil
        >>> if os.path.exists('./test-homedir'):
        ...     shutil.rmtree('./test-homedir')
        ...

        """

        super(GPG, self).__init__(
            binary=binary,
            home=homedir,
            keyring=keyring,
            secring=secring,
            default_preference_list=default_preference_list,
            options=options,
            verbose=verbose,
            use_agent=use_agent,)

        log.info("""
Initialised settings:
binary: %s
homedir: %s
keyring: %s
secring: %s
default_preference_list: %s
options: %s
verbose: %s
use_agent: %s
        """ % (self.binary, self.homedir, self.keyring, self.secring,
               self.default_preference_list, self.options, str(self.verbose),
               str(self.use_agent)))

        self._batch_dir = os.path.join(self.homedir, 'batch-files')
        self._key_dir  = os.path.join(self.homedir, 'generated-keys')

        #: The keyring used in the most recently created batch file
        self.temp_keyring = None
        #: The secring used in the most recently created batch file
        self.temp_secring = None

        ## check that everything runs alright:
        proc = self._open_subprocess(["--list-config", "--with-colons"])
        result = self._result_map['list'](self)
        self._collect_output(proc, result, stdin=proc.stdin)
        if proc.returncode != 0:
            raise RuntimeError("Error invoking gpg: %s: %s"
                               % (proc.returncode, result.stderr))

    def sign(self, data, **kwargs):
        """Create a signature for a message string or file.

        Note that this method is not for signing other keys. (In GnuPG's terms,
        what we all usually call 'keysigning' is actually termed
        'certification'...) Even though they are cryptographically the same
        operation, GnuPG differentiates between them, presumedly because these
        operations are also the same as the decryption operation. If the
        ``key_usage``s ``C (certification)``, ``S (sign)``, and ``E
        (encrypt)``, were all the same key, the key would "wear down" through
        frequent signing usage -- since signing data is usually done often --
        meaning that the secret portion of the keypair, also used for
        decryption in this scenario, would have a statistically higher
        probability of an adversary obtaining an oracle for it (or for a
        portion of the rounds in the cipher algorithm, depending on the family
        of cryptanalytic attack used).

        In simpler terms: this function isn't for signing your friends' keys,
        it's for something like signing an email.

        :type data: str or file
        :param data: A string or file stream to sign.
        :param str keyid: The key to sign with.
        :param str passphrase: The passphrase to pipe to stdin.
        :param bool clearsign: If True, create a cleartext signature.
        :param bool detach: If True, create a detached signature.
        :param bool binary: If True, do not ascii armour the output.
        """
        if 'default_key' in kwargs.items():
            log.info("Signing message '%r' with keyid: %s"
                     % (data, kwargs['default_key']))
        else:
            log.warn("No 'default_key' given! Using first key on secring.")

        if isinstance(data, file):
            result = self._sign_file(data, **kwargs)
        elif not _is_stream(data):
            stream = _make_binary_stream(data, self._encoding)
            result = self._sign_file(stream, **kwargs)
            stream.close()
        else:
            log.warn("Unable to sign message '%s' with type %s"
                     % (data, type(data)))
            result = None
        return result

    def _sign_file(self, file, default_key=None, passphrase=None,
                   clearsign=True, detach=False, binary=False):
        """Create a signature for a file.

        :param file: The file stream (i.e. it's already been open()'d) to sign.
        :param str keyid: The key to sign with.
        :param str passphrase: The passphrase to pipe to stdin.
        :param bool clearsign: If True, create a cleartext signature.
        :param bool detach: If True, create a detached signature.
        :param bool binary: If True, do not ascii armour the output.
        """
        log.debug("_sign_file():")
        if binary:
            log.info("Creating binary signature for file %s" % file)
            args = ['--sign']
        else:
            log.info("Creating ascii-armoured signature for file %s" % file)
            args = ['--sign --armor']

        if clearsign:
            args.append("--clearsign")
            if detach:
                log.warn("Cannot use both --clearsign and --detach-sign.")
                log.warn("Using default GPG behaviour: --clearsign only.")
        elif detach and not clearsign:
            args.append("--detach-sign")

        if default_key:
            args.append(str("--default-key %s" % default_key))

        ## We could use _handle_io here except for the fact that if the
        ## passphrase is bad, gpg bails and you can't write the message.
        result = self._result_map['sign'](self)
        proc = self._open_subprocess(args, passphrase is not None)
        try:
            if passphrase:
                _util._write_passphrase(proc.stdin, passphrase, self._encoding)
            writer = _util._threaded_copy_data(file, proc.stdin)
        except IOError as ioe:
            log.exception("Error writing message: %s" % ioe.message)
            writer = None
        self._collect_output(proc, result, writer, proc.stdin)
        return result

    def verify(self, data):
        """Verify the signature on the contents of the string ``data``.

        >>> gpg = GPG(homedir="keys")
        >>> input = gpg.gen_key_input(Passphrase='foo')
        >>> key = gpg.gen_key(input)
        >>> assert key
        >>> sig = gpg.sign('hello',keyid=key.fingerprint,passphrase='bar')
        >>> assert not sig
        >>> sig = gpg.sign('hello',keyid=key.fingerprint,passphrase='foo')
        >>> assert sig
        >>> verify = gpg.verify(sig.data)
        >>> assert verify

        """
        f = _make_binary_stream(data, self._encoding)
        result = self.verify_file(f)
        f.close()
        return result

    def verify_file(self, file, sig_file=None):
        """Verify the signature on the contents of a file or file-like
        object. Can handle embedded signatures as well as detached
        signatures. If using detached signatures, the file containing the
        detached signature should be specified as the ``sig_file``.

        :param file file: A file descriptor object. Its type will be checked
                          with :func:`_util._is_file`.
        :param str sig_file: A file containing the GPG signature data for
                             ``file``. If given, ``file`` is verified via this
                             detached signature.
        """

        fn = None
        result = self._result_map['verify'](self)

        if sig_file is None:
            log.debug("verify_file(): Handling embedded signature")
            args = ["--verify"]
            proc = self._open_subprocess(args)
            writer = _util._threaded_copy_data(file, proc.stdin)
            self._collect_output(proc, result, writer, stdin=proc.stdin)
        else:
            if not _util._is_file(sig_file):
                log.debug("verify_file(): '%r' is not a file" % sig_file)
                return result
            log.debug('verify_file(): Handling detached verification')
            sig_fh = None
            try:
                sig_fh = open(sig_file)
                args = ["--verify %s - " % sig_fh.name]
                proc = self._open_subprocess(args)
                writer = _util._threaded_copy_data(file, proc.stdin)
                self._collect_output(proc, result, stdin=proc.stdin)
            finally:
                if sig_fh and not sig_fh.closed:
                    sig_fh.close()
        return result

    def import_keys(self, key_data):
        """
        Import the key_data into our keyring.

        >>> import shutil
        >>> shutil.rmtree("doctests")
        >>> gpg = gnupg.GPG(homedir="doctests")
        >>> inpt = gpg.gen_key_input()
        >>> key1 = gpg.gen_key(inpt)
        >>> print1 = str(key1.fingerprint)
        >>> pubkey1 = gpg.export_keys(print1)
        >>> seckey1 = gpg.export_keys(print1,secret=True)
        >>> key2 = gpg.gen_key(inpt)
        >>> print2 = key2.fingerprint
        >>> seckeys = gpg.list_keys(secret=True)
        >>> pubkeys = gpg.list_keys()
        >>> assert print1 in seckeys.fingerprints
        >>> assert print1 in pubkeys.fingerprints
        >>> str(gpg.delete_keys(print1))
        'Must delete secret key first'
        >>> str(gpg.delete_keys(print1,secret=True))
        'ok'
        >>> str(gpg.delete_keys(print1))
        'ok'
        >>> pubkeys = gpg.list_keys()
        >>> assert not print1 in pubkeys.fingerprints
        >>> result = gpg.import_keys(pubkey1)
        >>> pubkeys = gpg.list_keys()
        >>> seckeys = gpg.list_keys(secret=True)
        >>> assert not print1 in seckeys.fingerprints
        >>> assert print1 in pubkeys.fingerprints
        >>> result = gpg.import_keys(seckey1)
        >>> assert result
        >>> seckeys = gpg.list_keys(secret=True)
        >>> assert print1 in seckeys.fingerprints
        """
        ## xxx need way to validate that key_data is actually a valid GPG key
        ##     it might be possible to use --list-packets and parse the output

        result = self._result_map['import'](self)
        log.info('Importing: %r', key_data[:256])
        data = _make_binary_stream(key_data, self._encoding)
        self._handle_io(['--import'], data, result, binary=True)
        data.close()
        return result

    def recv_keys(self, keyserver, *keyids):
        """Import a key from a keyserver

        >>> import shutil
        >>> shutil.rmtree("doctests")
        >>> gpg = gnupg.GPG(homedir="doctests")
        >>> result = gpg.recv_keys('pgp.mit.edu', '3FF0DB166A7476EA')
        >>> assert result

        """
        safe_keyserver = _fix_unsafe(keyserver)

        result = self._result_map['import'](self)
        data = _make_binary_stream("", self._encoding)
        args = ['--keyserver', keyserver, '--recv-keys']

        if keyids:
            if keyids is not None:
                safe_keyids = ' '.join(
                    [(lambda: _fix_unsafe(k))() for k in keyids])
                log.debug('recv_keys: %r', safe_keyids)
                args.extend(safe_keyids)

        self._handle_io(args, data, result, binary=True)
        data.close()
        log.debug('recv_keys result: %r', result.__dict__)
        return result

    def delete_keys(self, fingerprints, secret=False, subkeys=False):
        """Delete a key, or list of keys, from the current keyring.

        The keys must be refered to by their full fingerprint for GnuPG to
        delete them. If :param:`secret <secret=True>`, the corresponding secret
        keyring will be deleted from :attr:`GPG.secring <self.secring>`.

        :type fingerprints: str or list or tuple
        :param fingerprints: A string representing the fingerprint (or a
                             list/tuple of fingerprint strings) for the key(s)
                             to delete.

        :param bool secret: If True, delete the corresponding secret key(s)
                            also. (default: False)
        :param bool subkeys: If True, delete the secret subkey first, then
                             the public key. Same as
                            ``gpg --delete-secret-and-public-key 0x12345678``
                            (default: False)
        """

        which='keys'
        if secret:
            which='secret-key'
        if subkeys:
            which='secret-and-public-key'

        if _is_list_or_tuple(fingerprints):
            fingerprints = ' '.join(fingerprints)

        args = ['--batch']
        args.append("--delete-{} {}".format(which, fingerprints))

        result = self._result_map['delete'](self)
        p = self._open_subprocess(args)
        self._collect_output(p, result, stdin=p.stdin)
        return result

    def export_keys(self, keyids, secret=False, subkeys=False):
        """Export the indicated ``keyids``.

        :param str keyids: A keyid or fingerprint in any format that GnuPG will
                           accept.
        :param bool secret: If True, export only the secret key.
        :param bool subkeys: If True, export the secret subkeys.
        """
        which=''
        if subkeys:
            which='-secret-subkeys'
        elif secret:
            which='-secret-keys'

        if _is_list_or_tuple(keyids):
            keyids = ' '.join(['%s' % k for k in keyids])

        args = ["--armor"]
        args.append("--export{} {}".format(which, keyids))

        p = self._open_subprocess(args)
        ## gpg --export produces no status-fd output; stdout will be empty in
        ## case of failure
        #stdout, stderr = p.communicate()
        result = self._result_map['delete'](self) # any result will do
        self._collect_output(p, result, stdin=p.stdin)
        log.debug('Exported:%s%r' % (os.linesep, result.data))
        return result.data.decode(self._encoding, self._decode_errors)

    def list_keys(self, secret=False):
        """List the keys currently in the keyring.

        The GnuPG option '--show-photos', according to the GnuPG manual, "does
        not work with --with-colons", but since we can't rely on all versions
        of GnuPG to explicitly handle this correctly, we should probably
        include it in the args.

        >>> import shutil
        >>> shutil.rmtree("keys")
        >>> gpg = GPG(homedir="keys")
        >>> input = gpg.gen_key_input()
        >>> result = gpg.gen_key(input)
        >>> print1 = result.fingerprint
        >>> result = gpg.gen_key(input)
        >>> print2 = result.fingerprint
        >>> pubkeys = gpg.list_keys()
        >>> assert print1 in pubkeys.fingerprints
        >>> assert print2 in pubkeys.fingerprints
        """

        which='public-keys'
        if secret:
            which='secret-keys'
        args = "--list-%s --fixed-list-mode --fingerprint " % (which,)
        args += "--with-colons --list-options no-show-photos"
        args = [args]
        p = self._open_subprocess(args)

        # there might be some status thingumy here I should handle... (amk)
        # ...nope, unless you care about expired sigs or keys (stevegt)

        # Get the response information
        result = self._result_map['list'](self)
        self._collect_output(p, result, stdin=p.stdin)
        lines = result.data.decode(self._encoding,
                                   self._decode_errors).splitlines()
        valid_keywords = 'pub uid sec fpr sub'.split()
        for line in lines:
            if self.verbose:
                print(line)
            log.debug("%r", line.rstrip())
            if not line:
                break
            L = line.strip().split(':')
            if not L:
                continue
            keyword = L[0]
            if keyword in valid_keywords:
                getattr(result, keyword)(L)
        return result

    def list_packets(self, raw_data):
        """List the packet contents of a file."""
        args = ["--list-packets"]
        result = self._result_map['packets'](self)
        self._handle_io(args, _make_binary_stream(raw_data, self._encoding),
                        result)
        return result

    def list_sigs(self, *keyids):
        """Get the signatures for each of the ``keyids``.

        >>> import gnupg
        >>> gpg = gnupg.GPG(homedir="./tests/doctests")
        >>> key_input = gpg.gen_key_input()
        >>> key = gpg.gen_key(key_input)
        >>> assert key.fingerprint

        :rtype: dict
        :returns: A dictionary whose keys are the original keyid parameters,
                  and whose values are lists of signatures.
        """
        if len(keyids) > self._batch_limit:
            raise ValueError(
                "List signatures is limited to %d keyids simultaneously"
                % self._batch_limit)

        args = ["--with-colons", "--fixed-list-mode", "--list-sigs"]

        for key in keyids:
            args.append(key)

        proc = self._open_subprocess(args)

        result = self._result_map['list'](self)
        self._collect_output(proc, result, stdin=p.stdin)
        return result

    def gen_key(self, input):
        """Generate a GnuPG key through batch file key generation. See
        :meth:`GPG.gen_key_input()` for creating the control input.

        >>> import gnupg
        >>> gpg = gnupg.GPG(homedir="./tests/doctests")
        >>> key_input = gpg.gen_key_input()
        >>> key = gpg.gen_key(key_input)
        >>> assert key.fingerprint

        :param dict input: A dictionary of parameters and values for the new
                           key.
        :returns: The result mapping with details of the new key, which is a
                  :class:`parsers.GenKey <GenKey>` object.
        """
        ## see TODO file, tag :gen_key: for todo items
        args = ["--gen-key --batch"]
        key = self._result_map['generate'](self)
        f = _make_binary_stream(input, self._encoding)
        self._handle_io(args, f, key, binary=True)
        f.close()

        fpr = str(key.fingerprint)
        if len(fpr) == 20:
            if self.temp_keyring or self.temp_secring:
                if not os.path.exists(self._key_dir):
                    os.makedirs(self._key_dir)
                prefix = os.path.join(self._key_dir, fpr)

            if self.temp_keyring:
                if os.path.isfile(self.temp_keyring):
                    try: os.rename(self.temp_keyring, prefix+".pubring")
                    except OSError as ose: log.error(ose.message)
                    else: self.temp_keyring = None
                    #finally: self.import_keys(fpr)

            if self.temp_secring:
                if os.path.isfile(self.temp_secring):
                    try: os.rename(self.temp_secring, prefix+".secring")
                    except OSError as ose: log.error(ose.message)
                    else: self.temp_secring = None
                    #finally: self.import_keys(fpr)

        log.info("Key created. Fingerprint: %s" % fpr)
        return key

    def gen_key_input(self, separate_keyring=False, save_batchfile=False,
                      testing=False, **kwargs):
        """Generate a batch file for input to :meth:`GPG.gen_key()`.

        The GnuPG batch file key generation feature allows unattended key
        generation by creating a file with special syntax and then providing it
        to: ``gpg --gen-key --batch``. Batch files look like this:

            Name-Real: Alice
            Name-Email: alice@inter.net
            Expire-Date: 2014-04-01
            Key-Type: RSA
            Key-Length: 4096
            Key-Usage: cert
            Subkey-Type: RSA
            Subkey-Length: 4096
            Subkey-Usage: encrypt,sign,auth
            Passphrase: sekrit
            %pubring foo.gpg
            %secring sec.gpg
            %commit

        which is what this function creates for you. All of the available,
        non-control parameters are detailed below (control parameters are the
        ones which begin with a '%'). For example, to generate the batch file
        example above, use like this:

        >>> import gnupg
        GnuPG logging disabled...
        >>> from __future__ import print_function
        >>> gpg = gnupg.GPG(homedir='./tests/doctests')
        >>> alice = { 'name_real': 'Alice',
        ...     'name_email': 'alice@inter.net',
        ...     'expire_date': '2014-04-01',
        ...     'key_type': 'RSA',
        ...     'key_length': 4096,
        ...     'key_usage': '',
        ...     'subkey_type': 'RSA',
        ...     'subkey_length': 4096,
        ...     'subkey_usage': 'encrypt,sign,auth',
        ...     'passphrase': 'sekrit'}
        >>> alice_input = gpg.gen_key_input(**alice)
        >>> print(alice_input)
        Key-Type: RSA
        Subkey-Type: RSA
        Subkey-Usage: encrypt,sign,auth
        Expire-Date: 2014-04-01
        Passphrase: sekrit
        Name-Real: Alice
        Name-Email: alice@inter.net
        Key-Length: 4096
        Subkey-Length: 4096
        %pubring ./tests/doctests/pubring.gpg
        %secring ./tests/doctests/secring.gpg
        %commit
        <BLANKLINE>
        >>> alice_key = gpg.gen_key(alice_input)
        >>> assert alice_key is not None
        >>> assert alice_key.fingerprint is not None
        >>> message = "no one else can read my sekrit message"
        >>> encrypted = gpg.encrypt(message, alice_key.fingerprint)
        >>> assert isinstance(encrypted.data, str)

        :param bool separate_keyring: Specify for the new key to be written to
                                      a separate pubring.gpg and
                                      secring.gpg. If True,
                                      :meth:`GPG.gen_key` will automatically
                                      rename the separate keyring and secring
                                      to whatever the fingerprint of the
                                      generated key ends up being, suffixed
                                      with '.pubring' and '.secring'
                                      respectively.

        :param bool save_batchfile: Save a copy of the generated batch file to
                                    disk in a file named <name_real>.batch,
                                    where <name_real> is the ``name_real``
                                    parameter stripped of punctuation, spaces,
                                    and non-ascii characters.

        :param bool testing: Uses a faster, albeit insecure random number
                             generator to create keys. This should only be
                             used for testing purposes, for keys which are
                             going to be created and then soon after
                             destroyed, and never for the generation of actual
                             use keys.

        :param str name_real: The name field of the UID in the generated key.
        :param str name_comment: The comment in the UID of the generated key.
        :param str name_email: The email in the UID of the generated key.
                               (default: $USER@$(hostname) ) Remember to use
                               UTF-8 encoding for the entirety of the UID. At
                               least one of :param:`name_real <name_real>`,
                               :param:`name_comment <name_comment>`, or
                               :param:`name_email <name_email>` must be
                               provided, or else no user ID is created.

        :param str key_type: One of 'RSA', 'DSA', 'ELG-E', or 'default'.
                             (default: 'default') Starts a new parameter block
                             by giving the type of the primary key. The
                             algorithm must be capable of signing. This is a
                             required parameter. The algorithm may either be
                             an OpenPGP algorithm number or a string with the
                             algorithm name. The special value ‘default’ may
                             be used for algo to create the default key type;
                             in this case a :param:`key_usage <key_usage>`
                             should not be given and ‘default’ must also be
                             used for :param:`subkey_type <subkey_type>`.

        :param int key_length: The requested length of the generated key in
                               bits. (Default: 4096)

        :param str key_grip: hexstring This is an optional hexidecimal string
                             which is used to generate a CSR or certificate
                             for an already existing key. :param:key_length
                             will be ignored if this parameter is given.

        :param str key_usage: Space or comma delimited string of key
                              usages. Allowed values are ‘encrypt’, ‘sign’,
                              and ‘auth’. This is used to generate the key
                              flags. Please make sure that the algorithm is
                              capable of this usage. Note that OpenPGP
                              requires that all primary keys are capable of
                              certification, so no matter what usage is given
                              here, the ‘cert’ flag will be on. If no
                              ‘Key-Usage’ is specified and the ‘Key-Type’ is
                              not ‘default’, all allowed usages for that
                              particular algorithm are used; if it is not
                              given but ‘default’ is used the usage will be
                              ‘sign’.

        :param str subkey_type: This generates a secondary key
                                (subkey). Currently only one subkey can be
                                handled. See also ``key_type`` above.

        :param int subkey_length: The length of the secondary subkey in bits.

        :param str subkey_usage: Key usage for a subkey; similar to
                                 ``key_usage``.

        :type expire_date: int or str
        :param expire_date: Can be specified as an iso-date or as
                            <int>[d|w|m|y] Set the expiration date for the key
                            (and the subkey). It may either be entered in ISO
                            date format (2000-08-15) or as number of days,
                            weeks, month or years. The special notation
                            "seconds=N" is also allowed to directly give an
                            Epoch value. Without a letter days are
                            assumed. Note that there is no check done on the
                            overflow of the type used by OpenPGP for
                            timestamps. Thus you better make sure that the
                            given value make sense. Although OpenPGP works
                            with time intervals, GnuPG uses an absolute value
                            internally and thus the last year we can represent
                            is 2105.

        :param str creation_date: Set the creation date of the key as stored
                                  in the key information and which is also
                                  part of the fingerprint calculation. Either
                                  a date like "1986-04-26" or a full timestamp
                                  like "19860426T042640" may be used. The time
                                  is considered to be UTC. If it is not given
                                  the current time is used.

        :param str passphrase: The passphrase for the new key. The default is
                               to not use any passphrase. Note that
                               GnuPG>=2.1.x will not allow you to specify a
                               passphrase for batch key generation -- GnuPG
                               will ignore the ``passphrase`` parameter, stop,
                               and ask the user for the new passphrase.
                               However, we can put the command
                               '%no-protection' into the batch key generation
                               file to allow a passwordless key to be created,
                               which can then have its passphrase set later
                               with '--edit-key'.

        :param str preferences: Set the cipher, hash, and compression
                                preference values for this key. This expects
                                the same type of string as the sub-command
                                ‘setpref’ in the --edit-key menu.

        :param str revoker: Should be given as 'algo:fpr' [case sensitive].
                            Add a designated revoker to the generated
                            key. Algo is the public key algorithm of the
                            designated revoker (i.e. RSA=1, DSA=17, etc.) fpr
                            is the fingerprint of the designated revoker. The
                            optional ‘sensitive’ flag marks the designated
                            revoker as sensitive information. Only v4 keys may
                            be designated revokers.

        :param str keyserver: This is an optional parameter that specifies the
                              preferred keyserver URL for the key.

        :param str handle: This is an optional parameter only used with the
                           status lines KEY_CREATED and
                           KEY_NOT_CREATED. string may be up to 100 characters
                           and should not contain spaces. It is useful for
                           batch key generation to associate a key parameter
                           block with a status line.

        :rtype: str
        :returns: A suitable input string for the ``GPG.gen_key()`` method,
                  the latter of which will create the new keypair.

        see
        http://www.gnupg.org/documentation/manuals/gnupg-devel/Unattended-GPG-key-generation.html
        for more details.
        """

        parms = {}

        #: A boolean for determining whether to set subkey_type to 'default'
        default_type = False

        name_email = kwargs.get('name_email')
        uidemail = _util.create_uid_email(name_email)

        parms.setdefault('Key-Type', 'default')
        parms.setdefault('Key-Length', 4096)
        parms.setdefault('Name-Real', "Autogenerated Key")
        parms.setdefault('Expire-Date', _util._next_year())
        parms.setdefault('Name-Email', uidemail)

        if testing:
            ## This specific comment string is required by (some? all?)
            ## versions of GnuPG to use the insecure PRNG:
            parms.setdefault('Name-Comment', 'insecure!')

        for key, val in list(kwargs.items()):
            key = key.replace('_','-').title()
            ## to set 'cert', 'Key-Usage' must be blank string
            if not key in ('Key-Usage', 'Subkey-Usage'):
                if str(val).strip():
                    parms[key] = val

        ## if Key-Type is 'default', make Subkey-Type also be 'default'
        if parms['Key-Type'] == 'default':
            default_type = True
            for field in ('Key-Usage', 'Subkey-Usage',):
                try: parms.pop(field)  ## toss these out, handle manually
                except KeyError: pass

        ## Key-Type must come first, followed by length
        out  = "Key-Type: %s\n" % parms.pop('Key-Type')
        out += "Key-Length: %d\n" % parms.pop('Key-Length')
        if 'Subkey-Type' in parms.keys():
            out += "Subkey-Type: %s\n" % parms.pop('Subkey-Type')
        else:
            if default_type:
                out += "Subkey-Type: default\n"
        if 'Subkey-Length' in parms.keys():
            out += "Subkey-Length: %s\n" % parms.pop('Subkey-Length')

        for key, val in list(parms.items()):
            out += "%s: %s\n" % (key, val)

        ## There is a problem where, in the batch files, if the '%%pubring'
        ## and '%%secring' are given as any static string, i.e. 'pubring.gpg',
        ## that file will always get rewritten without confirmation, killing
        ## off any keys we had before. So in the case where we wish to
        ## generate a bunch of keys and then do stuff with them, we should not
        ## give 'pubring.gpg' as our keyring file, otherwise we will lose any
        ## keys we had previously.

        if separate_keyring:
            ring = str(uidemail + '_' + str(_util._utc_epoch()))
            self.temp_keyring = os.path.join(self.homedir, ring+'.pubring')
            self.temp_secring = os.path.join(self.homedir, ring+'.secring')
            out += "%%pubring %s\n" % self.temp_keyring
            out += "%%secring %s\n" % self.temp_secring

        if testing:
            ## see TODO file, tag :compatibility:gen_key_input:
            ##
            ## Add version detection before the '%no-protection' flag.
            out += "%no-protection\n"
            out += "%transient-key\n"

        out += "%commit\n"

        ## if we've been asked to save a copy of the batch file:
        if save_batchfile and parms['Name-Email'] != uidemail:
            asc_uid  = encodings.normalize_encoding(parms['Name-Email'])
            filename = _fix_unsafe(asc_uid) + _util._now() + '.batch'
            save_as  = os.path.join(self._batch_dir, filename)
            readme = os.path.join(self._batch_dir, 'README')

            if not os.path.exists(self._batch_dir):
                os.makedirs(self._batch_dir)

                ## the following pulls the link to GnuPG's online batchfile
                ## documentation from this function's docstring and sticks it
                ## in a README file in the batch directory:

                if getattr(self.gen_key_input, '__doc__', None) is not None:
                    docs = self.gen_key_input.__doc__
                else:
                    docs = str() ## docstring=None if run with "python -OO"
                links = '\n'.join(x.strip() for x in docs.splitlines()[-2:])
                explain = """
This directory was created by python-gnupg, on {}, and
it contains saved batch files, which can be given to GnuPG to automatically
generate keys. Please see
{}""".format(_util.now(), links) ## sometimes python is awesome.

                with open(readme, 'a+') as fh:
                    [fh.write(line) for line in explain]

            with open(save_as, 'a+') as batch_file:
                [batch_file.write(line) for line in out]

        return out

    def encrypt_file(self, filename, recipients,
                     default_key=None,
                     passphrase=None,
                     armor=True,
                     encrypt=True,
                     symmetric=False,
                     always_trust=True,
                     output=None,
                     cipher_algo='AES256',
                     digest_algo='SHA512',
                     compress_algo='ZLIB'):
        """Encrypt the message read from the file-like object ``filename``.

        :param str filename: The file or bytestream to encrypt.
        :param str recipients: The recipients to encrypt to. Recipients must
                               be specified keyID/fingerprint. Care should be
                               taken in Python2.x to make sure that the given
                               fingerprint is in fact a string and not a
                               unicode object.
        :param str default_key: The keyID/fingerprint of the key to use for
                                signing. If given, ``filename`` will be
                                encrypted and signed.
        :param bool always_trust: If True, ignore trust warnings on recipient
                                  keys. If False, display trust warnings.
                                  (default: True)
        :param str passphrase: If True, use this passphrase to unlock the
                                secret portion of the ``default_key`` for
                                signing.
        :param bool armor: If True, ascii armor the output; otherwise, the
                           output will be in binary format. (default: True)
        :param str output: The output file to write to. If not specified, the
                           encrypted output is returned, and thus should be
                           stored as an object in Python. For example:
        """
        args = []

        if output:
            if getattr(output, 'fileno', None) is not None:
                ## avoid overwrite confirmation message
                if getattr(output, 'name', None) is None:
                    if os.path.exists(output):
                        os.remove(output)
                    args.append('--output %s' % output)
                else:
                    if os.path.exists(output.name):
                        os.remove(output.name)
                    args.append('--output %s' % output.name)

        if armor: args.append('--armor')
        if always_trust: args.append('--always-trust')
        if cipher_algo: args.append('--cipher-algo %s' % cipher_algo)
        if compress_algo: args.append('--compress-algo %s' % compress_algo)

        if default_key:
            args.append('--sign')
            args.append('--default-key %s' % default_key)
            if digest_algo:
                args.append('--digest-algo %s' % digest_algo)

        ## both can be used at the same time for an encrypted file which
        ## is decryptable with a passphrase or secretkey.
        if symmetric: args.append('--symmetric')
        if encrypt: args.append('--encrypt')

        if len(recipients) >= 1:
            log.debug("GPG.encrypt() called for recipients '%s' with type '%s'"
                      % (recipients, type(recipients)))

            if isinstance(recipients, (list, tuple)):
                for recp in recipients:
                    if not _util._py3k:
                        if isinstance(recp, unicode):
                            try:
                                assert _parsers._is_hex(str(recp))
                            except AssertionError:
                                log.info("Can't accept recipient string: %s"
                                         % recp)
                            else:
                                args.append('--recipient %s' % str(recp))
                                continue
                            ## will give unicode in 2.x as '\uXXXX\uXXXX'
                            args.append('--recipient %r' % recp)
                            continue
                    if isinstance(recp, str):
                        args.append('--recipient %s' % recp)

            elif (not _util._py3k) and isinstance(recp, basestring):
                for recp in recipients.split('\x20'):
                    args.append('--recipient %s' % recp)

            elif _util._py3k and isinstance(recp, str):
                for recp in recipients.split(' '):
                    args.append('--recipient %s' % recp)
                    ## ...and now that we've proven py3k is better...

            else:
                log.debug("Don't know what to do with recipients: '%s'"
                          % recipients)

        result = self._result_map['crypt'](self)
        log.debug("Got filename '%s' with type '%s'."
                  % (filename, type(filename)))
        self._handle_io(args, filename, result,
                        passphrase=passphrase, binary=True)
        log.debug('GPG.encrypt_file(): Result: %r', result.data)
        return result

    def encrypt(self, data, *recipients, **kwargs):
        """Encrypt the message contained in ``data`` to ``recipients``.

        >>> import shutil
        >>> if os.path.exists("keys"):
        ...     shutil.rmtree("keys")
        >>> gpg = GPG(homedir="keys")
        >>> input = gpg.gen_key_input(passphrase='foo')
        >>> result = gpg.gen_key(input)
        >>> print1 = result.fingerprint
        >>> input = gpg.gen_key_input()
        >>> result = gpg.gen_key(input)
        >>> print2 = result.fingerprint
        >>> result = gpg.encrypt("hello",print2)
        >>> message = str(result)
        >>> assert message != 'hello'
        >>> result = gpg.decrypt(message)
        >>> assert result
        >>> str(result)
        'hello'
        >>> result = gpg.encrypt("hello again",print1)
        >>> message = str(result)
        >>> result = gpg.decrypt(message,passphrase='bar')
        >>> result.status in ('decryption failed', 'bad passphrase')
        True
        >>> assert not result
        >>> result = gpg.decrypt(message,passphrase='foo')
        >>> result.status == 'decryption ok'
        True
        >>> str(result)
        'hello again'
        >>> result = gpg.encrypt("signed hello",print2,sign=print1,passphrase='foo')
        >>> result.status == 'encryption ok'
        True
        >>> message = str(result)
        >>> result = gpg.decrypt(message)
        >>> result.status == 'decryption ok'
        True
        >>> assert result.fingerprint == print1

        """
        stream = _make_binary_stream(data, self._encoding)
        result = self.encrypt_file(stream, recipients, **kwargs)
        stream.close()
        return result

    def decrypt(self, message, **kwargs):
        """Decrypt the contents of a string or file-like object ``message``.

        :param message: A string or file-like object to decrypt.
        """
        stream = _make_binary_stream(message, self._encoding)
        result = self.decrypt_file(stream, **kwargs)
        stream.close()
        return result

    def decrypt_file(self, filename, always_trust=False, passphrase=None,
                     output=None):
        """
        Decrypt the contents of a file-like object :param:file .

        :param file: A file-like object to decrypt.
        :param always_trust: Instruct GnuPG to ignore trust checks.
        :param passphrase: The passphrase for the secret key used for decryption.
        :param output: A file to write the decrypted output to.
        """
        args = ["--decrypt"]
        if output:  # write the output to a file with the specified name
            if os.path.exists(output):
                os.remove(output) # to avoid overwrite confirmation message
            args.append('--output %s' % output)
        if always_trust:
            args.append("--always-trust")
        result = self._result_map['crypt'](self)
        self._handle_io(args, filename, result, passphrase, binary=True)
        log.debug('decrypt result: %r', result.data)
        return result


class GPGWrapper(GPG):
    """
    This is a temporary class for handling GPG requests, and should be
    replaced by a more general class used throughout the project.
    """
    import re

    def find_key_by_email(self, email, secret=False):
        """
        Find user's key based on their email.
        """
        for key in self.list_keys(secret=secret):
            for uid in key['uids']:
                if re.search(email, uid):
                    return key
        raise LookupError("GnuPG public key for email %s not found!" % email)

    def find_key_by_subkey(self, subkey):
        for key in self.list_keys():
            for sub in key['subkeys']:
                if sub[0] == subkey:
                    return key
        raise LookupError(
            "GnuPG public key for subkey %s not found!" % subkey)

    def encrypt(self, data, recipient, default_key=None, always_trust=True,
                passphrase=None, symmetric=False):
        """
        Encrypt data using GPG.
        """
        # TODO: devise a way so we don't need to "always trust".
        return super(GPGWrapper, self).encrypt(data, recipient,
                                               default_key=default_key,
                                               always_trust=always_trust,
                                               passphrase=passphrase,
                                               symmetric=symmetric,
                                               cipher_algo='AES256')

    def send_keys(self, keyserver, *keyids):
        """Send keys to a keyserver."""
        result = self._result_map['list'](self)
        log.debug('send_keys: %r', keyids)
        data = _util._make_binary_stream("", self._encoding)
        args = ['--keyserver', keyserver, '--send-keys']
        args.extend(keyids)
        self._handle_io(args, data, result, binary=True)
        log.debug('send_keys result: %r', result.__dict__)
        data.close()
        return result

    def encrypted_to(self, raw_data):
        """
        Return the key to which raw_data is encrypted to.
        """
        # TODO: make this support multiple keys.
        result = self.list_packets(raw_data)
        if not result.key:
            raise LookupError(
                "Content is not encrypted to a GnuPG key!")
        try:
            return self.find_key_by_keyid(result.key)
        except:
            return self.find_key_by_subkey(result.key)

    def is_encrypted_sym(self, raw_data):
        result = self.list_packets(raw_data)
        return bool(result.need_passphrase_sym)

    def is_encrypted_asym(self, raw_data):
        result = self.list_packets(raw_data)
        return bool(result.key)

    def is_encrypted(self, raw_data):
        self.is_encrypted_asym() or self.is_encrypted_sym()
